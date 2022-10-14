# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.


import logging
import math
from dataclasses import dataclass

import torch

from xformers import _is_triton_available
from xformers.components.attention import Attention, AttentionConfig, register_attention

_is_blocksparse_available = _is_triton_available()
if _is_blocksparse_available:
    from triton.ops.blocksparse import matmul as blocksparse_matmul  # type: ignore
    from triton.ops.blocksparse import softmax as blocksparse_softmax  # type: ignore

    from xformers.triton.utils import gpu_capabilities_older_than_70

    # Blocksparse requires Tensor cores
    if gpu_capabilities_older_than_70():
        logging.warning(
            "Blocksparse is not available: the current GPU does not expose Tensor cores"
        )
        _is_blocksparse_available = False


if _is_blocksparse_available:

    @dataclass
    class BlockSparseAttentionConfig(AttentionConfig):
        layout: torch.Tensor  # The dimensions of the random features
        block_size: int
        dropout: float
        num_heads: int

    @register_attention("blocksparse", BlockSparseAttentionConfig)
    class BlockSparseAttention(Attention):
        r"""
        Thin wrap over the Triton blocksparse computations. The sparsity pattern is determined through the layout.

        .. warning: the layout is assumed to have the dimensions [heads, seq, seq].
            If some dimensions are missing, we assume that the same layout is to be used across heads.

        .. warning: for now, the sequence (context) length has to be a power of two. This constraint could
            be relaxed in the future.

        .. warning: the block size has to be picked from [16, 32, 64]. Some speed is gained from bigger blocks.
            It is of course possible to reproduce coarser patterns given these primitives, as the user sees fit.

        """

        def __init__(
            self,
            layout: torch.Tensor,
            block_size: int = 16,
            dropout: float = 0.0,
            num_heads: int = 1,  # optional, used to adapt the layout if in need
            causal: bool = False,
            *args,
            **kwargs,
        ):
            if layout.dim() == 2:
                logging.warning(
                    "The layout passed is lacking a head dimension and a batch dimension"
                )
                logging.warning(
                    "Now assuming that the same layout is to be used across all heads"
                )
                layout = layout.unsqueeze(0).expand(num_heads, -1, -1)
                logging.warning(f"New layout dimensions: {layout.shape}")

            assert block_size in (
                16,
                32,
                64,
                128,
            ), "Only block sizes in [16, 32, 64, 128] are supported"

            super().__init__()

            self.causal = causal

            self.attn_drop = torch.nn.Dropout(dropout, inplace=False)

            # Pure blocksparse data
            self.layout = layout
            self.block_size = block_size

            # make sure that the head dimension is not folded down with the batch
            self.requires_head_dimension = True

            # key padding mask and attention mask must be passed in separately
            self.requires_same_k_q_dimensions = True

            # The underlying triton op does not support per element attention mask
            self.supports_attention_mask = True
            self.supports_key_padding_mask = False

        def create_triton_kernels(self, device):
            # blocksparse operators
            self.sparse_dot_sdd = blocksparse_matmul(
                self.layout,
                self.block_size,
                "sdd",
                trans_a=False,
                trans_b=True,
                device=device,
            )

            self.sparse_dot_dsd = blocksparse_matmul(
                self.layout,
                self.block_size,
                "dsd",
                trans_a=False,
                trans_b=False,
                device=device,
            )

            self.sparse_softmax = blocksparse_softmax(
                self.layout,
                self.block_size,
                device=device,
            )

            self.non_zero_block_idx = self.layout.nonzero().T.to(device)

        def forward(
            self,
            q: torch.Tensor,
            k: torch.Tensor,
            v: torch.Tensor,
            scale: float = 1.0,
            att_mask: torch.tensor = None,
            *args,
            **kwargs,
        ) -> torch.Tensor:

            r"""
            A thin wrap around the Triton blockparse attention operation

            .. note: Per element attention mask is not supported, but you can specify causality
            """

            # Delayed triton init, to make sure that we get the right device
            # Infer device from query
            if not hasattr(self, "sparse_dot_sdd"):
                self.create_triton_kernels(q.device)

            assert (
                q.shape[-2] == k.shape[-2]
            ), "Blocksparse requires the same dimensions for K and Q for now"

            assert (
                q.shape[-2] == self.layout.shape[-2] * self.block_size
            ), "Actual sequence size and layout are inconsistent"
            assert (
                k.shape[-2] == self.layout.shape[-2] * self.block_size
            ), "Actual sequence size and layout are inconsistent"

            assert (
                q.shape[-2] % self.block_size
            ) == 0, "Sequence length {}  must be a multiple of block size {}".format(
                q.shape[-2], self.block_size
            )

            # Blocksparse only works on fp16
            q_dtype = q.dtype
            q, k, v = q.half(), k.half(), v.half()

            # Self-attend: (B, nh, S, hs) x (B, nh, hs, S) -> (B, nh, S, S)
            # When the computations are block sparse, the matrix types change along the way:
            # - (sparse) attention matrix = (dense) Kt * (dense) Q
            q = q / math.sqrt(q.size(-1))
            sparse_att_mat = self.sparse_dot_sdd(q, k)

            # apply masks
            if att_mask is not None:
                # mask shape is (B, nh, S, S)
                assert (
                    att_mask.shape[0] == q.shape[0]
                    and att_mask.shape[1] == q.shape[1]
                    and att_mask.shape[2] == q.shape[-2]
                    and att_mask.shape[3] == q.shape[-2]
                )

                # reshape input mask into blocks (B, nh, S, S) -> (B, nh, vertical_blocks, horizontal_blocks, block_size, block_size)
                block_att_mask = att_mask.reshape(
                    att_mask.shape[0],
                    att_mask.shape[1],
                    att_mask.shape[-1] // self.block_size,
                    att_mask.shape[-1] // self.block_size,
                    self.block_size,
                    self.block_size,
                )
                
                # gather based on predefined layout. non_zero_block_idx is (head, block_row, block_col)
                block_att_mask = block_att_mask[:, self.non_zero_block_idx[0], self.non_zero_block_idx[1], self.non_zero_block_idx[2]]

                sparse_att_mat += block_att_mask

            # - softmax on the sparse attention matrix
            sparse_att_mat = self.sparse_softmax(
                sparse_att_mat, scale=scale, is_causal=self.causal
            )

            sparse_att_mat = self.attn_drop(sparse_att_mat)

            # - then (dense) attention is (sparse) attention matrix * dense (value)
            a = self.sparse_dot_dsd(sparse_att_mat, v)
            return a.to(q_dtype)
