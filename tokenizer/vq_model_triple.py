# Modified from:
#   taming-transformers: https://github.com/CompVis/taming-transformers
#   maskgit: https://github.com/google-research/maskgit
from dataclasses import dataclass, field
from typing import List
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange


@dataclass
class ModelArgs:
    codebook_size: int = 16384
    codebook_embed_dim: int = 8

    codebook_l2_norm: bool = True
    codebook_show_usage: bool = True
    commit_loss_beta: float = 0.25
    entropy_loss_ratio: float = 0.0
    
    encoder_ch_mult: List[int] = field(default_factory=lambda: [1, 1, 2, 2, 4])
    decoder_ch_mult: List[int] = field(default_factory=lambda: [1, 1, 2, 2, 4])
    z_channels: int = 256
    dropout_p: float = 0.0

    with_clip_supervision: bool = False
    with_disentanglement: bool = False
    disentanglement_ratio: float = 0.0


class ResidualAttentionBlock(nn.Module):
    def __init__(
            self,
            d_model,
            n_head,
            mlp_ratio=4.0,
            act_layer=nn.GELU,
            norm_layer=nn.LayerNorm
    ):
        super().__init__()

        self.ln_1 = norm_layer(d_model)
        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.mlp_ratio = mlp_ratio
        # optionally we can disable the FFN
        if mlp_ratio > 0:
            self.ln_2 = norm_layer(d_model)
            mlp_width = int(d_model * mlp_ratio)
            self.mlp = nn.Sequential(OrderedDict([
                ("c_fc", nn.Linear(d_model, mlp_width)),
                ("gelu", act_layer()),
                ("c_proj", nn.Linear(mlp_width, d_model))
            ]))

    def attention(
            self,
            x: torch.Tensor
    ):
        return self.attn(x, x, x, need_weights=False)[0]

    def forward(
            self,
            x: torch.Tensor,
    ):
        attn_output = self.attention(x=self.ln_1(x))
        x = x + attn_output
        if self.mlp_ratio > 0:
            x = x + self.mlp(self.ln_2(x))
        return x


def _expand_token(token, batch_size: int):
    return token.unsqueeze(0).expand(batch_size, -1, -1)


class FactorizedAdapter(nn.Module):
    def __init__(self, down_factor):
        super().__init__()

        self.grid_size = 256 // down_factor  # image size // down-sample ration
        self.width = 512                     # use the same dim as the output of the VQ encoder
        self.num_layers = 6
        self.num_heads = 8

        scale = self.width ** -0.5
        self.positional_embedding = nn.Parameter(scale * torch.randn(self.grid_size ** 2, self.width))
        self.ln_pre = nn.LayerNorm(self.width)
        self.transformer = nn.ModuleList()
        for i in range(self.num_layers):
            self.transformer.append(ResidualAttentionBlock(
                self.width, self.num_heads, mlp_ratio=4.0
            ))
        self.ln_post = nn.LayerNorm(self.width)

    def forward(self, x):
        h = x.shape[-1]
        x = rearrange(x, 'b c h w -> b (h w) c')

        x = x + self.positional_embedding.to(x.dtype)  # shape = [*, grid ** 2, width]

        x = self.ln_pre(x)
        x = x.permute(1, 0, 2)          # NLD -> LND
        for i in range(self.num_layers):
            x = self.transformer[i](x)
        x = x.permute(1, 0, 2)          # LND -> NLD
        x = self.ln_post(x)

        x = rearrange(x, 'b (h w) c -> b c h w', h=h)

        return x


class FeatPredHead(nn.Module):
    def __init__(self, input_dim=256, out_dim=1024, down_factor=16):
        super().__init__()

        self.grid_size = 256 // down_factor  # image size // down-sample ration
        self.width = out_dim               # subject to CLIP/DINO model
        self.num_layers = 3
        self.num_heads = 8

        in_dim = input_dim
        out_dim = self.width
        self.upscale = nn.Sequential(
            nn.Linear(in_dim, out_dim),
            nn.ReLU(),
            nn.Linear(out_dim, out_dim)
        )

        scale = self.width ** -0.5
        self.class_embedding = nn.Parameter(scale * torch.randn(1, self.width))
        self.positional_embedding = nn.Parameter(scale * torch.randn(self.grid_size ** 2 + 1, self.width))
        self.ln_pre = nn.LayerNorm(self.width)
        self.transformer = nn.ModuleList()
        for i in range(self.num_layers):
            self.transformer.append(ResidualAttentionBlock(
                self.width, self.num_heads, mlp_ratio=4.0
            ))
        self.ln_post = nn.LayerNorm(self.width)

    def forward(self, x):
        x = rearrange(x, 'b c h w -> b (h w) c')
        x = self.upscale(x)  # NLD

        x = torch.cat([_expand_token(self.class_embedding, x.shape[0]).to(x.dtype), x], dim=1)
        x = x + self.positional_embedding.to(x.dtype)  # shape = [*, 1 + grid ** 2, width]

        x = self.ln_pre(x)
        x = x.permute(1, 0, 2)  # NLD -> LND
        for i in range(self.num_layers):
            x = self.transformer[i](x)
        x = x.permute(1, 0, 2)  # LND -> NLD
        x = self.ln_post(x)

        return x


class VQModel(nn.Module):
    def __init__(self, config: ModelArgs):
        super().__init__()
        self.config = config

        # Two head encoder
        self.encoder = Encoder(ch_mult=config.encoder_ch_mult, z_channels=config.z_channels, dropout=config.dropout_p)

        # Quantizer for visual detail head
        self.quantize_vis = VectorQuantizer(config.codebook_size, config.codebook_embed_dim,
                                            config.commit_loss_beta, config.entropy_loss_ratio,
                                            config.codebook_l2_norm, config.codebook_show_usage)
        self.quant_conv_vis = nn.Conv2d(config.z_channels, config.codebook_embed_dim, 1)

        # Quantizer for mid-level semantic head
        self.quantize_sem_mid = VectorQuantizer(config.codebook_size, config.codebook_embed_dim,
                                                config.commit_loss_beta, config.entropy_loss_ratio,
                                                config.codebook_l2_norm, config.codebook_show_usage)
        self.quant_conv_sem_mid = nn.Conv2d(config.z_channels, config.codebook_embed_dim, 1)

        # Quantizer for high-level semantic head
        self.quantize_sem_high = VectorQuantizer(config.codebook_size, config.codebook_embed_dim,
                                                 config.commit_loss_beta, config.entropy_loss_ratio,
                                                 config.codebook_l2_norm, config.codebook_show_usage)
        self.quant_conv_sem_high = nn.Conv2d(config.z_channels, config.codebook_embed_dim, 1)

        print("Visual codebook: [{} x {}]".format(config.codebook_size, config.codebook_embed_dim))
        print("Mid Semantic codebook: [{} x {}]".format(config.codebook_size, config.codebook_embed_dim))
        print("High Semantic codebook: [{} x {}]".format(config.codebook_size, config.codebook_embed_dim))

        # Pixel decoder
        input_dim = config.codebook_embed_dim * 3
        self.post_quant_conv = nn.Conv2d(input_dim, config.z_channels, 1)
        self.decoder = Decoder(ch_mult=config.decoder_ch_mult, z_channels=config.z_channels,
                               dropout=config.dropout_p)

        self.num_resolutions = len(config.encoder_ch_mult)
        if self.num_resolutions == 5:
            down_factor = 16
        elif self.num_resolutions == 4:
            down_factor = 8
        else:
            raise NotImplementedError

        # Semantic feature prediction
        if self.config.with_clip_supervision:
            print("Include feature prediction head for representation supervision")
            self.mid_sem_feat_pred = FeatPredHead(input_dim=config.codebook_embed_dim, out_dim=384, down_factor=down_factor)
            self.high_sem_feat_pred = FeatPredHead(input_dim=config.codebook_embed_dim, out_dim=768, down_factor=down_factor)
        else:
            print("NO representation supervision")

        if self.config.with_disentanglement:
            print("Disentangle Ratio: ", self.config.disentanglement_ratio)
        else:
            print("No Disentangle Regularization")

    def compute_disentangle_loss(self, quant_vis, quant_sem):
        quant_vis = rearrange(quant_vis, 'b c h w -> (b h w) c')
        quant_sem = rearrange(quant_sem, 'b c h w -> (b h w) c')

        quant_vis = F.normalize(quant_vis, p=2, dim=-1)
        quant_sem = F.normalize(quant_sem, p=2, dim=-1)

        dot_product = torch.sum(quant_vis * quant_sem, dim=1)
        loss = torch.mean(dot_product ** 2) * self.config.disentanglement_ratio

        return loss

    def forward(self, input):
        h_vis, h_sem_mid, h_sem_high = self.encoder(input)
        h_vis = self.quant_conv_vis(h_vis)
        h_sem_mid = self.quant_conv_sem_mid(h_sem_mid)
        h_sem_high = self.quant_conv_sem_high(h_sem_high)

        quant_vis, emb_loss_vis, _ = self.quantize_vis(h_vis)
        quant_sem_mid, emb_loss_sem_mid, _ = self.quantize_sem_mid(h_sem_mid)
        quant_sem_high, emb_loss_sem_high, _ = self.quantize_sem_high(h_sem_high)

        if self.config.with_clip_supervision:
            mid_lvl_sem_feat = self.mid_sem_feat_pred(quant_sem_mid)
            high_lvl_sem_feat = self.high_sem_feat_pred(quant_sem_high)
        else:
            mid_lvl_sem_feat = None
            high_lvl_sem_feat = None

        if self.config.with_disentanglement:
            disentangle_loss = (self.compute_disentangle_loss(quant_vis, quant_sem_mid) +
                                self.compute_disentangle_loss(quant_vis, quant_sem_high) +
                                self.compute_disentangle_loss(quant_sem_mid, quant_sem_high)) / 3.0
        else:
            disentangle_loss = 0

        quant = torch.cat([quant_vis, quant_sem_mid, quant_sem_high], dim=1)
        dec = self.decode(quant)

        return dec, emb_loss_vis, emb_loss_sem_mid, emb_loss_sem_high, \
        disentangle_loss, mid_lvl_sem_feat, high_lvl_sem_feat

    def encode(self, x):
        h_vis, h_sem_mid, h_sem_high = self.encoder(x)
        h_vis = self.quant_conv_vis(h_vis)
        h_sem_mid = self.quant_conv_sem_mid(h_sem_mid)
        h_sem_high = self.quant_conv_sem_high(h_sem_high)

        quant_vis, emb_loss_vis, info_vis = self.quantize_vis(h_vis)
        quant_sem_mid, emb_loss_sem_mid, info_sem_mid = self.quantize_sem_mid(h_sem_mid)
        quant_sem_high, emb_loss_sem_high, info_sem_high = self.quantize_sem_high(h_sem_high)

        return quant_vis, quant_sem_mid, quant_sem_high, info_vis, info_sem_mid, info_sem_high

    def decode_code(self, indices_vis, indices_sem_mid, indices_sem_high,
                    shape_vis=None, shape_sem_mid=None, shape_sem_high=None, channel_first=True):
        quant_vis = self.quantize_vis.get_codebook_entry(indices_vis, shape_vis, channel_first)
        quant_sem_mid = self.quantize_sem_mid.get_codebook_entry(indices_sem_mid, shape_sem_mid, channel_first)
        quant_sem_high = self.quantize_sem_high.get_codebook_entry(indices_sem_high, shape_sem_high, channel_first)

        quant = torch.cat([quant_vis, quant_sem_mid, quant_sem_high], dim=1)
        dec = self.decode(quant)

        return dec

    def decode(self, quant):
        quant = self.post_quant_conv(quant)
        dec = self.decoder(quant)
        return dec


class Encoder(nn.Module):
    def __init__(self, in_channels=3, ch=128, ch_mult=(1,1,2,2,4), num_res_blocks=2, 
                 norm_type='group', dropout=0.0, resamp_with_conv=True, z_channels=256):
        super().__init__()
        self.num_resolutions = len(ch_mult)
        self.num_res_blocks = num_res_blocks
        self.conv_in = nn.Conv2d(in_channels, ch, kernel_size=3, stride=1, padding=1)

        # downsampling
        in_ch_mult = (1,) + tuple(ch_mult)
        self.conv_blocks = nn.ModuleList()
        for i_level in range(self.num_resolutions):
            conv_block = nn.Module()
            # res & attn
            res_block = nn.ModuleList()
            attn_block = nn.ModuleList()
            block_in = ch*in_ch_mult[i_level]
            block_out = ch*ch_mult[i_level]
            for _ in range(self.num_res_blocks):
                res_block.append(ResnetBlock(block_in, block_out, dropout=dropout, norm_type=norm_type))
                block_in = block_out
                if i_level == self.num_resolutions - 1:
                    attn_block.append(AttnBlock(block_in, norm_type))
            conv_block.res = res_block
            conv_block.attn = attn_block
            # downsample
            if i_level != self.num_resolutions-1:
                conv_block.downsample = Downsample(block_in, resamp_with_conv)
            self.conv_blocks.append(conv_block)

        # middle
        self.mid = nn.ModuleList()
        self.mid.append(ResnetBlock(block_in, block_in, dropout=dropout, norm_type=norm_type))
        self.mid.append(AttnBlock(block_in, norm_type=norm_type))
        self.mid.append(ResnetBlock(block_in, block_in, dropout=dropout, norm_type=norm_type))


        if self.num_resolutions == 5:
            down_factor = 16
        elif self.num_resolutions == 4:
            down_factor = 8
        else:
            raise NotImplementedError

        # semantic head mid-level
        self.semantic_head_mid = nn.ModuleList()
        self.semantic_head_mid.append(FactorizedAdapter(down_factor))

        # semantic head high-level
        self.semantic_head_high = nn.ModuleList()
        self.semantic_head_high.append(FactorizedAdapter(down_factor))

        # visual details head
        self.visual_head = nn.ModuleList()
        self.visual_head.append(FactorizedAdapter(down_factor))

        # end
        self.norm_out_sem_mid = Normalize(block_in, norm_type)
        self.conv_out_sem_mid = nn.Conv2d(block_in, z_channels, kernel_size=3, stride=1, padding=1)

        self.norm_out_sem_high = Normalize(block_in, norm_type)
        self.conv_out_sem_high = nn.Conv2d(block_in, z_channels, kernel_size=3, stride=1, padding=1)

        self.norm_out_vis = Normalize(block_in, norm_type)
        self.conv_out_vis = nn.Conv2d(block_in, z_channels, kernel_size=3, stride=1, padding=1)

    def forward(self, x):
        h = self.conv_in(x)
        # downsampling
        for i_level, block in enumerate(self.conv_blocks):
            for i_block in range(self.num_res_blocks):
                h = block.res[i_block](h)
                if len(block.attn) > 0:
                    h = block.attn[i_block](h)
            if i_level != self.num_resolutions - 1:
                h = block.downsample(h)
        
        # middle
        for mid_block in self.mid:
            h = mid_block(h)

        h_vis = h
        h_sem_mid = h
        h_sem_high = h

        # semantic head mid-level
        for blk in self.semantic_head_mid:
            h_sem_mid = blk(h_sem_mid)
        h_sem_mid = self.norm_out_sem_mid(h_sem_mid)
        h_sem_mid = nonlinearity(h_sem_mid)
        h_sem_mid = self.conv_out_sem_mid(h_sem_mid)

        # semantic head high-level
        for blk in self.semantic_head_high:
            h_sem_high = blk(h_sem_high)
        h_sem_high = self.norm_out_sem_high(h_sem_high)
        h_sem_high = nonlinearity(h_sem_high)
        h_sem_high = self.conv_out_sem_high(h_sem_high)

        # visual head
        for blk in self.visual_head:
            h_vis = blk(h_vis)
        h_vis = self.norm_out_vis(h_vis)
        h_vis = nonlinearity(h_vis)
        h_vis = self.conv_out_vis(h_vis)

        return h_vis, h_sem_mid, h_sem_high


class Decoder(nn.Module):
    def __init__(self, z_channels=256, ch=128, ch_mult=(1,1,2,2,4), num_res_blocks=2, norm_type="group",
                 dropout=0.0, resamp_with_conv=True, out_channels=3):
        super().__init__()
        self.num_resolutions = len(ch_mult)
        self.num_res_blocks = num_res_blocks

        block_in = ch*ch_mult[self.num_resolutions-1]
        # z to block_in
        self.conv_in = nn.Conv2d(z_channels, block_in, kernel_size=3, stride=1, padding=1)

        # middle
        self.mid = nn.ModuleList()
        self.mid.append(ResnetBlock(block_in, block_in, dropout=dropout, norm_type=norm_type))
        self.mid.append(AttnBlock(block_in, norm_type=norm_type))
        self.mid.append(ResnetBlock(block_in, block_in, dropout=dropout, norm_type=norm_type))

        # upsampling
        self.conv_blocks = nn.ModuleList()
        for i_level in reversed(range(self.num_resolutions)):
            conv_block = nn.Module()
            # res & attn
            res_block = nn.ModuleList()
            attn_block = nn.ModuleList()
            block_out = ch*ch_mult[i_level]
            for _ in range(self.num_res_blocks + 1):
                res_block.append(ResnetBlock(block_in, block_out, dropout=dropout, norm_type=norm_type))
                block_in = block_out
                if i_level == self.num_resolutions - 1:
                    attn_block.append(AttnBlock(block_in, norm_type))
            conv_block.res = res_block
            conv_block.attn = attn_block
            # downsample
            if i_level != 0:
                conv_block.upsample = Upsample(block_in, resamp_with_conv)
            self.conv_blocks.append(conv_block)

        # end
        self.norm_out = Normalize(block_in, norm_type)
        self.conv_out = nn.Conv2d(block_in, out_channels, kernel_size=3, stride=1, padding=1)

    @property
    def last_layer(self):
        return self.conv_out.weight
    
    def forward(self, z):
        # z to block_in
        h = self.conv_in(z)

        # middle
        for mid_block in self.mid:
            h = mid_block(h)
        
        # upsampling
        for i_level, block in enumerate(self.conv_blocks):
            for i_block in range(self.num_res_blocks + 1):
                h = block.res[i_block](h)
                if len(block.attn) > 0:
                    h = block.attn[i_block](h)
            if i_level != self.num_resolutions - 1:
                h = block.upsample(h)

        # end
        h = self.norm_out(h)
        h = nonlinearity(h)
        h = self.conv_out(h)

        return h


class VectorQuantizer(nn.Module):
    def __init__(self, n_e, e_dim, beta, entropy_loss_ratio, l2_norm, show_usage):
        super().__init__()
        self.n_e = n_e
        self.e_dim = e_dim
        self.beta = beta
        self.entropy_loss_ratio = entropy_loss_ratio
        self.l2_norm = l2_norm
        self.show_usage = show_usage

        self.embedding = nn.Embedding(self.n_e, self.e_dim)
        self.embedding.weight.data.uniform_(-1.0 / self.n_e, 1.0 / self.n_e)
        if self.l2_norm:
            self.embedding.weight.data = F.normalize(self.embedding.weight.data, p=2, dim=-1)
        if self.show_usage:
            self.register_buffer("codebook_used", nn.Parameter(torch.zeros(65536)))     # 1048576

    def forward(self, z):
        # reshape z -> (batch, height, width, channel) and flatten
        z = torch.einsum('b c h w -> b h w c', z).contiguous()
        z_flattened = z.view(-1, self.e_dim)
        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z

        if self.l2_norm:
            z = F.normalize(z, p=2, dim=-1)
            z_flattened = F.normalize(z_flattened, p=2, dim=-1)
            embedding = F.normalize(self.embedding.weight, p=2, dim=-1)
        else:
            embedding = self.embedding.weight

        d = torch.sum(z_flattened ** 2, dim=1, keepdim=True) + \
            torch.sum(embedding**2, dim=1) - 2 * \
            torch.einsum('bd,dn->bn', z_flattened, torch.einsum('n d -> d n', embedding))

        min_encoding_indices = torch.argmin(d, dim=1)
        z_q = embedding[min_encoding_indices].view(z.shape)
        perplexity = None
        min_encodings = None
        vq_loss = None
        commit_loss = None
        entropy_loss = None
        codebook_usage = 0

        if self.show_usage and self.training:
            cur_len = min_encoding_indices.shape[0]
            self.codebook_used[:-cur_len] = self.codebook_used[cur_len:].clone()
            self.codebook_used[-cur_len:] = min_encoding_indices
            codebook_usage = len(torch.unique(self.codebook_used)) / self.n_e

        # compute loss for embedding
        if self.training:
            vq_loss = torch.mean((z_q - z.detach()) ** 2) 
            commit_loss = self.beta * torch.mean((z_q.detach() - z) ** 2) 
            entropy_loss = self.entropy_loss_ratio * compute_entropy_loss(-d)

        # preserve gradients
        z_q = z + (z_q - z).detach()

        # reshape back to match original input shape
        z_q = torch.einsum('b h w c -> b c h w', z_q)

        return z_q, (vq_loss, commit_loss, entropy_loss, codebook_usage), (perplexity, min_encodings, min_encoding_indices)

    def get_codebook_entry(self, indices, shape=None, channel_first=True):
        # shape = (batch, channel, height, width) if channel_first else (batch, height, width, channel)
        if self.l2_norm:
            embedding = F.normalize(self.embedding.weight, p=2, dim=-1)
        else:
            embedding = self.embedding.weight
        z_q = embedding[indices]  # (b*h*w, c)

        if shape is not None:
            if channel_first:
                z_q = z_q.reshape(shape[0], shape[2], shape[3], shape[1])
                # reshape back to match original input shape
                z_q = z_q.permute(0, 3, 1, 2).contiguous()
            else:
                z_q = z_q.view(shape)
        return z_q


class ResnetBlock(nn.Module):
    def __init__(self, in_channels, out_channels=None, conv_shortcut=False, dropout=0.0, norm_type='group'):
        super().__init__()
        self.in_channels = in_channels
        out_channels = in_channels if out_channels is None else out_channels
        self.out_channels = out_channels
        self.use_conv_shortcut = conv_shortcut

        self.norm1 = Normalize(in_channels, norm_type)
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=1)
        self.norm2 = Normalize(out_channels, norm_type)
        self.dropout = nn.Dropout(dropout)
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1)

        if self.in_channels != self.out_channels:
            if self.use_conv_shortcut:
                self.conv_shortcut = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=1)
            else:
                self.nin_shortcut = nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=1, padding=0)

    def forward(self, x):
        h = x
        h = self.norm1(h)
        h = nonlinearity(h)
        h = self.conv1(h)
        h = self.norm2(h)
        h = nonlinearity(h)
        h = self.dropout(h)
        h = self.conv2(h)

        if self.in_channels != self.out_channels:
            if self.use_conv_shortcut:
                x = self.conv_shortcut(x)
            else:
                x = self.nin_shortcut(x)
        return x+h


class AttnBlock(nn.Module):
    def __init__(self, in_channels, norm_type='group'):
        super().__init__()
        self.norm = Normalize(in_channels, norm_type)
        self.q = nn.Conv2d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)
        self.k = nn.Conv2d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)
        self.v = nn.Conv2d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)
        self.proj_out = nn.Conv2d(in_channels, in_channels, kernel_size=1, stride=1, padding=0)

    def forward(self, x):
        h_ = x
        h_ = self.norm(h_)
        q = self.q(h_)
        k = self.k(h_)
        v = self.v(h_)

        # compute attention
        b,c,h,w = q.shape
        q = q.reshape(b,c,h*w)
        q = q.permute(0,2,1)   # b,hw,c
        k = k.reshape(b,c,h*w) # b,c,hw
        w_ = torch.bmm(q,k)     # b,hw,hw    w[b,i,j]=sum_c q[b,i,c]k[b,c,j]
        w_ = w_ * (int(c)**(-0.5))
        w_ = F.softmax(w_, dim=2)

        # attend to values
        v = v.reshape(b,c,h*w)
        w_ = w_.permute(0,2,1)   # b,hw,hw (first hw of k, second of q)
        h_ = torch.bmm(v,w_)     # b, c,hw (hw of q) h_[b,c,j] = sum_i v[b,c,i] w_[b,i,j]
        h_ = h_.reshape(b,c,h,w)

        h_ = self.proj_out(h_)

        return x+h_


def nonlinearity(x):
    # swish
    return x*torch.sigmoid(x)


def Normalize(in_channels, norm_type='group'):
    assert norm_type in ['group', 'batch']
    if norm_type == 'group':
        return nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)
    elif norm_type == 'batch':
        return nn.SyncBatchNorm(in_channels)


class Upsample(nn.Module):
    def __init__(self, in_channels, with_conv):
        super().__init__()
        self.with_conv = with_conv
        if self.with_conv:
            self.conv = nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=1, padding=1)

    def forward(self, x):
        x = F.interpolate(x, scale_factor=2.0, mode="nearest")
        if self.with_conv:
            x = self.conv(x)
        return x


class Downsample(nn.Module):
    def __init__(self, in_channels, with_conv):
        super().__init__()
        self.with_conv = with_conv
        if self.with_conv:
            # no asymmetric padding in torch conv, must do it ourselves
            self.conv = nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=2, padding=0)

    def forward(self, x):
        if self.with_conv:
            pad = (0,1,0,1)
            x = F.pad(x, pad, mode="constant", value=0)
            x = self.conv(x)
        else:
            x = F.avg_pool2d(x, kernel_size=2, stride=2)
        return x


def compute_entropy_loss(affinity, loss_type="softmax", temperature=0.01):
    flat_affinity = affinity.reshape(-1, affinity.shape[-1])
    flat_affinity /= temperature
    probs = F.softmax(flat_affinity, dim=-1)
    log_probs = F.log_softmax(flat_affinity + 1e-5, dim=-1)
    if loss_type == "softmax":
        target_probs = probs
    else:
        raise ValueError("Entropy loss {} not supported".format(loss_type))
    avg_probs = torch.mean(target_probs, dim=0)
    avg_entropy = - torch.sum(avg_probs * torch.log(avg_probs + 1e-5))
    sample_entropy = - torch.mean(torch.sum(target_probs * log_probs, dim=-1))
    loss = sample_entropy - avg_entropy
    return loss


#################################################################################
#                              VQ Model Configs                                 #
#################################################################################
def VQ_8(**kwargs):
    return VQModel(ModelArgs(encoder_ch_mult=[1, 2, 2, 4], decoder_ch_mult=[1, 2, 2, 4], **kwargs))


def VQ_16(**kwargs):
    return VQModel(ModelArgs(encoder_ch_mult=[1, 1, 2, 2, 4], decoder_ch_mult=[1, 1, 2, 2, 4], **kwargs))


VQ_models = {'VQ-16': VQ_16, 'VQ-8': VQ_8}
