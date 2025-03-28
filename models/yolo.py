# YOLOv5 🚀 by Ultralytics, GPL-3.0 license
"""
YOLO-specific modules

Usage:
    $ python path/to/models/yolo.py --cfg yolov5s.yaml
"""

import argparse
import os
import platform
import sys
from copy import deepcopy
from pathlib import Path

import torch

FILE = Path(__file__).resolve()
ROOT = FILE.parents[1]  # YOLOv5 root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
if platform.system() != 'Windows':
    ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative

from models.common import *
from models.experimental import *
from utils.autoanchor import check_anchor_order
from utils.general import LOGGER, check_version, check_yaml, make_divisible, print_args
from utils.plots import feature_visualization
from utils.torch_utils import (fuse_conv_and_bn, initialize_weights, model_info, profile, scale_img, select_device,
                               time_sync)

try:
    import thop  # for FLOPs computation
except ImportError:
    thop = None


class Detect(nn.Module):
    stride = None  # strides computed during build
    onnx_dynamic = False  # ONNX export parameter
    export = False  # export mode

    def __init__(self, nc=80, anchors=(), ch=(), inplace=True):  # detection layer
        super().__init__()
        self.nc = nc  # number of classes
        self.no = nc + 5  # number of outputs per anchor
        self.nl = len(anchors)  # number of detection layers
        self.na = len(anchors[0]) // 2  # number of anchors
        self.grid = [torch.zeros(1)] * self.nl  # init grid
        self.anchor_grid = [torch.zeros(1)] * self.nl  # init anchor grid
        self.register_buffer('anchors', torch.tensor(anchors).float().view(self.nl, -1, 2))  # shape(nl,na,2)
        self.m = nn.ModuleList(nn.Conv2d(x, self.no * self.na, 1) for x in ch)  # output conv
        self.inplace = inplace  # use in-place ops (e.g. slice assignment)

    def forward(self, x):
        z = []  # inference output
        for i in range(self.nl):
            x[i] = self.m[i](x[i])  # conv
            bs, _, ny, nx = x[i].shape  # x(bs,255,20,20) to x(bs,3,20,20,85)
            x[i] = x[i].view(bs, self.na, self.no, ny, nx).permute(0, 1, 3, 4, 2).contiguous()

            if not self.training:  # inference
                if self.onnx_dynamic or self.grid[i].shape[2:4] != x[i].shape[2:4]:
                    self.grid[i], self.anchor_grid[i] = self._make_grid(nx, ny, i)

                y = x[i].sigmoid()
                if self.inplace:
                    y[..., 0:2] = (y[..., 0:2] * 2 + self.grid[i]) * self.stride[i]  # xy
                    y[..., 2:4] = (y[..., 2:4] * 2) ** 2 * self.anchor_grid[i]  # wh
                else:  # for YOLOv5 on AWS Inferentia https://github.com/ultralytics/yolov5/pull/2953
                    xy, wh, conf = y.split((2, 2, self.nc + 1), 4)  # y.tensor_split((2, 4, 5), 4)  # torch 1.8.0
                    xy = (xy * 2 + self.grid[i]) * self.stride[i]  # xy
                    wh = (wh * 2) ** 2 * self.anchor_grid[i]  # wh
                    y = torch.cat((xy, wh, conf), 4)
                z.append(y.view(bs, -1, self.no))

        return x if self.training else (torch.cat(z, 1),) if self.export else (torch.cat(z, 1), x)

    def _make_grid(self, nx=20, ny=20, i=0):
        d = self.anchors[i].device
        t = self.anchors[i].dtype
        shape = 1, self.na, ny, nx, 2  # grid shape
        y, x = torch.arange(ny, device=d, dtype=t), torch.arange(nx, device=d, dtype=t)
        if check_version(torch.__version__, '1.10.0'):  # torch>=1.10.0 meshgrid workaround for torch>=0.7 compatibility
            yv, xv = torch.meshgrid(y, x, indexing='ij')
        else:
            yv, xv = torch.meshgrid(y, x)
        grid = torch.stack((xv, yv), 2).expand(shape) - 0.5  # add grid offset, i.e. y = 2.0 * x - 0.5
        anchor_grid = (self.anchors[i] * self.stride[i]).view((1, self.na, 1, 1, 2)).expand(shape)
        return grid, anchor_grid


class Decouple(nn.Module):
    # Decoupled convolution
    def __init__(self, c1, nc=80, na=3):  # ch_in, num_classes, num_anchors
        super().__init__()
        c_ = min(c1, 256)  # min(c1, nc * na)
        self.na = na  # number of anchors
        self.nc = nc  # number of classes
        self.a = Conv(c1, c_, 1)
        c = [int(x + na * 5) for x in (c_ - na * 5) * torch.linspace(1, 0, 4)]  # linear channel descent

        self.b1, self.b2, self.b3 = Conv(c_, c[1], 3), Conv(c[1], c[2], 3), nn.Conv2d(c[2], na * 5, 1)  # vc

        self.c1, self.c2, self.c3 = Conv(c_, c_, 1), Conv(c_, c_, 1), nn.Conv2d(c_, na * nc, 1)  # cls

    def forward(self, x):
        bs, nc, ny, nx = x.shape  # BCHW
        x = self.a(x)
        b = self.b3(self.b2(self.b1(x)))
        c = self.c3(self.c2(self.c1(x)))
        return torch.cat((b.view(bs, self.na, 5, ny, nx), c.view(bs, self.na, self.nc, ny, nx)), 2).view(bs, -1, ny, nx)


class Decoupled_Detect(nn.Module):
    stride = None  # strides computed during build
    onnx_dynamic = False  # ONNX export parameter
    export = False  # export mode

    def __init__(self, nc=80, anchors=(), ch=(), inplace=True):  # detection layer
        super().__init__()

        self.nc = nc  # number of classes
        self.no = nc + 5  # number of outputs per anchor
        self.nl = len(anchors)  # number of detection layers
        self.na = len(anchors[0]) // 2  # number of anchors
        self.grid = [torch.zeros(1)] * self.nl  # init grid
        self.anchor_grid = [torch.zeros(1)] * self.nl  # init anchor grid
        self.register_buffer('anchors', torch.tensor(anchors).float().view(self.nl, -1, 2))  # shape(nl,na,2)
        self.m = nn.ModuleList(
            Decouple(x, self.nc, self.na) for x in ch)  # yolov5 provide ,  old Decouple too much FLOP
        self.inplace = inplace  # use in-place ops (e.g. slice assignment)

    def forward(self, x):
        z = []  # inference output
        for i in range(self.nl):
            x[i] = self.m[i](x[i])  # conv
            bs, _, ny, nx = x[i].shape  # x(bs,255,20,20) to x(bs,3,20,20,85)
            x[i] = x[i].view(bs, self.na, self.no, ny, nx).permute(0, 1, 3, 4, 2).contiguous()

            if not self.training:  # inference
                if self.onnx_dynamic or self.grid[i].shape[2:4] != x[i].shape[2:4]:
                    self.grid[i], self.anchor_grid[i] = self._make_grid(nx, ny, i)

                y = x[i].sigmoid()
                if self.inplace:
                    y[..., 0:2] = (y[..., 0:2] * 2 + self.grid[i]) * self.stride[i]  # xy
                    y[..., 2:4] = (y[..., 2:4] * 2) ** 2 * self.anchor_grid[i]  # wh
                else:  # for YOLOv5 on AWS Inferentia https://github.com/ultralytics/yolov5/pull/2953
                    xy, wh, conf = y.split((2, 2, self.nc + 1), 4)  # y.tensor_split((2, 4, 5), 4)  # torch 1.8.0
                    xy = (xy * 2 + self.grid[i]) * self.stride[i]  # xy
                    wh = (wh * 2) ** 2 * self.anchor_grid[i]  # wh
                    y = torch.cat((xy, wh, conf), 4)
                z.append(y.view(bs, -1, self.no))

        return x if self.training else (torch.cat(z, 1),) if self.export else (torch.cat(z, 1), x)

    def _make_grid(self, nx=20, ny=20, i=0, torch_1_10=check_version(torch.__version__, '1.10.0')):
        d = self.anchors[i].device
        t = self.anchors[i].dtype
        shape = 1, self.na, ny, nx, 2  # grid shape
        y, x = torch.arange(ny, device=d, dtype=t), torch.arange(nx, device=d, dtype=t)
        yv, xv = torch.meshgrid(y, x, indexing='ij') if torch_1_10 else torch.meshgrid(y, x)  # torch>=0.7 compatibility
        grid = torch.stack((xv, yv), 2).expand(shape) - 0.5  # add grid offset, i.e. y = 2.0 * x - 0.5
        anchor_grid = (self.anchors[i] * self.stride[i]).view((1, self.na, 1, 1, 2)).expand(shape)
        return grid, anchor_grid


class Model(nn.Module):
    # YOLOv5 model
    def __init__(self, cfg='yolov5s.yaml', ch=3, ch2=3, nc=None, anchors=None):  # model, input channels, number of classes
        super().__init__()
        if isinstance(cfg, dict):
            self.yaml = cfg  # model dict
        else:  # is *.yaml
            import yaml  # for torch hub
            self.yaml_file = Path(cfg).name
            with open(cfg, encoding='ascii', errors='ignore') as f:
                self.yaml = yaml.safe_load(f)  # model dict

        # Define model
        ch = self.yaml['ch'] = self.yaml.get('ch', ch)  # input channels
        ch2 = self.yaml['ch2'] = self.yaml.get('ch2', ch2)
        if nc and nc != self.yaml['nc']:
            LOGGER.info(f"Overriding model.yaml nc={self.yaml['nc']} with nc={nc}")
            self.yaml['nc'] = nc  # override yaml value
        if anchors:
            LOGGER.info(f'Overriding model.yaml anchors with anchors={anchors}')
            self.yaml['anchors'] = round(anchors)  # override yaml value
        self.model, self.save, self.backbonedepth,self.backbone1depth = parse_model(deepcopy(self.yaml), ch=[ch], ch2=[ch2])  # model, savelist
        self.names = [str(i) for i in range(self.yaml['nc'])]  # default names
        self.inplace = self.yaml.get('inplace', True)

        # Build strides, anchors
        m = self.model[-1]  # Detect()
        if isinstance(m, Detect):
            s = 512  # 2x min stride
            # s = 256
            m.inplace = self.inplace
            m.stride = torch.tensor([s / x.shape[-2] for x in self.forward(torch.zeros(1, ch, s, s),torch.zeros(1, ch2, s, s))])  # forward
            check_anchor_order(m)  # must be in pixel-space (not grid-space)
            m.anchors /= m.stride.view(-1, 1, 1)
            self.stride = m.stride
            self._initialize_biases()  # only run once
        elif isinstance(m, Decoupled_Detect):
            s = 512  # 2x min stride
            m.inplace = self.inplace
            m.stride = torch.tensor([s / x.shape[-2] for x in self.forward(torch.zeros(1, ch, s, s),torch.zeros(1, ch2, s, s))])   # forward
            check_anchor_order(m)  # must be in pixel-space (not grid-space)
            m.anchors /= m.stride.view(-1, 1, 1)
            self.stride = m.stride
            self._initialize_dh_biases()  # only run once


        # Init weights, biases
        initialize_weights(self)
        self.info()
        LOGGER.info('')

    def forward(self, x1, x2, augment=False, profile=False, visualize=False):
        if augment:
            return self._forward_augment(x1, x2)  # augmented inference, None
        return self._forward_once(x1, x2, profile, visualize)  # single-scale inference, train

    def _forward_augment(self, x1, x2):
        img_size = x1.shape[-2:]  # height, width
        s = [1, 0.83, 0.67]  # scales
        f = [None, 3, None]  # flips (2-ud, 3-lr)
        y = []  # outputs
        for si, fi in zip(s, f):
            x1i = scale_img(x1.flip(fi) if fi else x1, si, gs=int(self.stride.max()))
            x2i = scale_img(x2.flip(fi) if fi else x2, si, gs=int(self.stride.max()))
            yi = self._forward_once(x1i, x2i)[0]  # forward
            # cv2.imwrite(f'img_{si}.jpg', 255 * xi[0].cpu().numpy().transpose((1, 2, 0))[:, :, ::-1])  # save
            yi = self._descale_pred(yi, fi, si, img_size)
            y.append(yi)
        y = self._clip_augmented(y)  # clip augmented tails
        return torch.cat(y, 1), None  # augmented inference, train

    # def _forward_once(self, x1, x2, profile=False, visualize=False):
    #     y,y1,y2, dt = [], [], [], []  # outputs
    #     for m in self.model:
    #         if self.backbonedepth <= m.i < self.backbone1depth + self.backbonedepth:
    #             # if m.type == 'models.common.Diff':
    #             #     x1 = y1[m.f[0]]
    #             #     x2 = y2[m.f[1]]
    #             #     x2 = m(x1, x2)
    #             #     y2.append(x2 if m.i in self.save else None)
    #             #     y = y2
    #             # else:
    #             #     x1 = y1[m.f[0]]
    #             #     x2 = y2[m.f[1]]
    #             #     x2 = m(x1, x2)
    #             #     y2.append(x2 if m.i in self.save else None)
    #             #     y = y2
    #             x1 = y1[m.f[0]]
    #             x2 = y2[m.f[1]]
    #             x2 = m(x1, x2)
    #             y2.append(x2 if m.i in self.save else None)
    #             y = y2
    #         elif m.i < self.backbonedepth:
    #             x1 = m(x1)
    #             x2 = m(x2)
    #             y1.append(x1 if m.i in self.save else None)  # save output
    #             y2.append(x2 if m.i in self.save else None)
    #             y = y2
    #         else:
    #             # print(m.i)
    #             # print(m.type)
    #             if m.f != -1:  # if not from previous layer
    #                 x2 = y[m.f] if isinstance(m.f, int) else [x2 if j == -1 else y[j] for j in m.f]  # from earlier layers
    #             if profile:
    #                 self._profile_one_layer(m, x2, dt)
    #             # print(x2.type())
    #             x2 = m(x2)  # run
    #             y.append(x2 if m.i in self.save else None)  # save output
    #         if visualize:
    #                 feature_visualization(x2, m.type, m.i, save_dir=visualize)
    #
    #     return x2

    def _forward_once(self, x1, x2, profile=False, visualize=False):
        y, y1, y2, dt = [], [], [], []  # outputs
        for m in self.model:
            if self.backbonedepth <= m.i < self.backbone1depth + self.backbonedepth:
                if m.type in ('models.common.Concat'):
                    x2 = [y1[m.f[0]], y2[m.f[1]]]
                    if profile:
                        self._profile_one_layer(m, x2, dt)
                    x2 = m(x2)  # run
                    y2.append(x2 if m.i in self.save else None)
                    y = y2
                else:
                    x1 = y1[m.f[0]]
                    x2 = y2[m.f[1]]
                    x2 = m(x1, x2)
                    y2.append(x2 if m.i in self.save else None)
                    y = y2
            elif m.i < self.backbonedepth:
                if m.type in ('models.common.Diff'):
                    x1 = y1[m.f[0]]
                    x2 = y2[m.f[1]]
                    x = m(x1, x2)
                    y1.append(x if m.i in self.save else None)  # save output
                    y2.append(x if m.i in self.save else None)
                    y = y2
                elif m.type in ('models.common.Exchange_Feature', 'models.common.Exchange_Feature_nometric'):
                    x1 = y1[m.f[0]]
                    x2 = y2[m.f[1]]
                    x3, x4 = m(x1, x2)
                    y1.append(x3 if m.i in self.save else None)  # save output
                    y2.append(x4 if m.i in self.save else None)
                    y = y2
                else:
                    x1 = m(x1)
                    x2 = m(x2)
                    y1.append(x1 if m.i in self.save else None)  # save output
                    y2.append(x2 if m.i in self.save else None)
                    y = y2

            else:
                if m.f != -1:  # if not from previous layer
                    x2 = y[m.f] if isinstance(m.f, int) else [x2 if j == -1 else y[j] for j in
                                                              m.f]  # from earlier layers
                if profile:
                    self._profile_one_layer(m, x2, dt)
                x2 = m(x2)  # run
                y.append(x2 if m.i in self.save else None)  # save output
            if visualize:
                    feature_visualization(x2, m.type, m.i, save_dir=visualize)

        return x2

    def _descale_pred(self, p, flips, scale, img_size):
        # de-scale predictions following augmented inference (inverse operation)
        if self.inplace:
            p[..., :4] /= scale  # de-scale
            if flips == 2:
                p[..., 1] = img_size[0] - p[..., 1]  # de-flip ud
            elif flips == 3:
                p[..., 0] = img_size[1] - p[..., 0]  # de-flip lr
        else:
            x, y, wh = p[..., 0:1] / scale, p[..., 1:2] / scale, p[..., 2:4] / scale  # de-scale
            if flips == 2:
                y = img_size[0] - y  # de-flip ud
            elif flips == 3:
                x = img_size[1] - x  # de-flip lr
            p = torch.cat((x, y, wh, p[..., 4:]), -1)
        return p

    def _clip_augmented(self, y):
        # Clip YOLOv5 augmented inference tails
        nl = self.model[-1].nl  # number of detection layers (P3-P5)
        g = sum(4 ** x for x in range(nl))  # grid points
        e = 1  # exclude layer count
        i = (y[0].shape[1] // g) * sum(4 ** x for x in range(e))  # indices
        y[0] = y[0][:, :-i]  # large
        i = (y[-1].shape[1] // g) * sum(4 ** (nl - 1 - x) for x in range(e))  # indices
        y[-1] = y[-1][:, i:]  # small
        return y

    def _profile_one_layer(self, m, x, dt):
        c = isinstance(m, Detect)  # is final layer, copy input as inplace fix
        o = thop.profile(m, inputs=(x.copy() if c else x,), verbose=False)[0] / 1E9 * 2 if thop else 0  # FLOPs
        t = time_sync()
        for _ in range(10):
            m(x.copy() if c else x)
        dt.append((time_sync() - t) * 100)
        if m == self.model[0]:
            LOGGER.info(f"{'time (ms)':>10s} {'GFLOPs':>10s} {'params':>10s}  module")
        LOGGER.info(f'{dt[-1]:10.2f} {o:10.2f} {m.np:10.0f}  {m.type}')
        if c:
            LOGGER.info(f"{sum(dt):10.2f} {'-':>10s} {'-':>10s}  Total")

    def _initialize_biases(self, cf=None):  # initialize biases into Detect(), cf is class frequency
        # https://arxiv.org/abs/1708.02002 section 3.3
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1.
        m = self.model[-1]  # Detect() module
        for mi, s in zip(m.m, m.stride):  # from
            b = mi.bias.view(m.na, -1).detach()  # conv.bias(255) to (3,85)
            b[:, 4] += math.log(8 / (640 / s) ** 2)  # obj (8 objects per 640 image)
            b[:, 5:] += math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # cls
            mi.bias = torch.nn.Parameter(b.view(-1), requires_grad=True)

    def _initialize_dh_biases(self, cf=None):  # initialize biases into Detect(), cf is class frequency
        # https://arxiv.org/abs/1708.02002 section 3.3
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1.
        m = self.model[-1]  # Detect() module
        for mi, s in zip(m.m, m.stride):  # from
            # reg_bias = mi.reg_preds.bias.view(m.na, -1).detach()
            # reg_bias += math.log(8 / (640 / s) ** 2)
            # mi.reg_preds.bias = torch.nn.Parameter(reg_bias.view(-1), requires_grad=True)

            # cls_bias = mi.cls_preds.bias.view(m.na, -1).detach()
            # cls_bias += math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # cls
            # mi.cls_preds.bias = torch.nn.Parameter(cls_bias.view(-1), requires_grad=True)
            b = mi.b3.bias.view(m.na, -1)
            b.data[:, 4] += math.log(8 / (640 / s) ** 2)  # obj (8 objects per 640 image)
            mi.b3.bias = torch.nn.Parameter(b.view(-1), requires_grad=True)
            b = mi.c3.bias.data
            b += math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # cls
            mi.c3.bias = torch.nn.Parameter(b, requires_grad=True)



    def _print_biases(self):
        m = self.model[-1]  # Detect() module
        for mi in m.m:  # from
            b = mi.bias.detach().view(m.na, -1).T  # conv.bias(255) to (3,85)
            LOGGER.info(
                ('%6g Conv2d.bias:' + '%10.3g' * 6) % (mi.weight.shape[1], *b[:5].mean(1).tolist(), b[5:].mean()))

    # def _print_weights(self):
    #     for m in self.model.modules():
    #         if type(m) is Bottleneck:
    #             LOGGER.info('%10.3g' % (m.w.detach().sigmoid() * 2))  # shortcut weights

    def fuse(self):  # fuse model Conv2d() + BatchNorm2d() layers
        LOGGER.info('Fusing layers... ')
        for m in self.model.modules():
            if isinstance(m, (Conv, DWConv)) and hasattr(m, 'bn'):
                m.conv = fuse_conv_and_bn(m.conv, m.bn)  # update conv
                delattr(m, 'bn')  # remove batchnorm
                m.forward = m.forward_fuse  # update forward
        self.info()
        return self

    def info(self, verbose=False, img_size=640):  # print model information
        model_info(self, verbose, img_size)


    def _apply(self, fn):
        # Apply to(), cpu(), cuda(), half() to model tensors that are not parameters or registered buffers
        self = super()._apply(fn)
        m = self.model[-1]  # Detect()
        if isinstance(m, (Detect, Decoupled_Detect)):
            m.stride = fn(m.stride)
            m.grid = list(map(fn, m.grid))
            if isinstance(m.anchor_grid, list):
                m.anchor_grid = list(map(fn, m.anchor_grid))
        return self



def parse_model(d, ch, ch2):  # model_dict, input_channels(3)
    LOGGER.info(f"\n{'':>3}{'from':>18}{'n':>3}{'params':>10}  {'module':<40}{'arguments':<30}")
    anchors, nc, gd, gw, backbonedepth, backbone1depth = d['anchors'], d['nc'], d['depth_multiple'], d['width_multiple'], d['backbonedepth'], d['backbone1depth']
    na = (len(anchors[0]) // 2) if isinstance(anchors, list) else anchors  # number of anchors
    no = na * (nc + 5)  # number of outputs = anchors * (classes + 5)

    # c1：公共部分的输入通道数 ch 公共的模块的保存的输出通道数
    # c12:backbone1+backbone+head的输入通道数 c22 backbone1+backbone+head的输出通道数 ch2保存backbone1+backbone+head的输出通道数
    layers, save, c2, c22 = [], [], ch[-1], ch2[-1]   # layers保存每一层的结构, savelist记录下所有层结构中from中不是-1的层结构序号, ch out 保存当前层的输出channel
    for i, (f, n, m, args) in enumerate(d['backbone'] + d['backbone1'] + d['head']):  # from, number, module, args
        m = eval(m) if isinstance(m, str) else m  # eval strings
        for j, a in enumerate(args):
            try:
                args[j] = eval(a) if isinstance(a, str) else a  # eval strings
            except NameError:
                pass

        n = n_ = max(round(n * gd), 1) if n > 1 else n  # depth gain
        if i < backbonedepth:
            if m in (Conv, GhostConv, Bottleneck, GhostBottleneck, SPP, SPPF, DWConv, MixConv2d, Focus, CrossConv,
                     BottleneckCSP, C3, C3TR, C3SPP, C3Ghost, nn.ConvTranspose2d, DWConvTranspose2d, C3x, Diff,
                     C2f, Exchange_Feature, GAT_FeatureHW_try):
                if m in (Diff, Exchange_Feature, ):
                    c1 = ch[f[0]]
                    c2 = ch[f[1]]
                    args = [c1, c2]
                else:
                    c1, c2 = ch[f], args[0]
                    if c2 != no:  # if not output
                        c2 = make_divisible(c2 * gw, 8)

                    args = [c1, c2, *args[1:]]
                if m in [BottleneckCSP, C3, C3TR, C3Ghost, C3x, C2f]:
                    args.insert(2, n)  # number of repeats
                    n = 1

        elif backbonedepth <= i < backbonedepth + backbone1depth:
            if m in (Diff, Merge, DGAM):
                c1 = ch[f[0]]
                c2 = ch[f[1]]
                args = [c1, c2]
            elif m in [Concat]:
                c2 = sum(ch2[x] for x in f)

        else:
            if m in (Conv, GhostConv, Bottleneck, GhostBottleneck, SPP, SPPF, DWConv, MixConv2d, Focus, CrossConv,
                     BottleneckCSP, C3, C3TR, C3SPP, C3Ghost, nn.ConvTranspose2d, DWConvTranspose2d, C3x, C2f):
                c12, c22 = ch2[f], args[0]
                if c22 != no:  # if not output
                    c22 = make_divisible(c22 * gw, 8)

                args = [c12, c22, *args[1:]]
                if m in [BottleneckCSP, C3, C3TR, C3Ghost, C3x, C2f]:
                    args.insert(2, n)  # number of repeats
                    n = 1
            elif m is nn.BatchNorm2d:
                args = [ch2[f]]
            elif m in [Concat]:
                c22 = sum(ch2[x] for x in f)
            elif m in {Detect, Decoupled_Detect}:
                args.append([ch2[x] for x in f])
                if isinstance(args[1], int):  # number of anchors
                    args[1] = [list(range(args[1] * 2))] * len(f)
            elif m is Contract:
                c22 = ch2[f] * args[0] ** 2
            elif m is Expand:
                c22 = ch2[f] // args[0] ** 2
            else:
                c22 = ch2[f]

        m_ = nn.Sequential(*(m(*args) for _ in range(n))) if n > 1 else m(*args)  # module
        t = str(m)[8:-2].replace('__main__.', '')  # module type
        np = sum(x.numel() for x in m_.parameters())  # number params
        m_.i, m_.f, m_.type, m_.np = i, f, t, np  # attach index, 'from' index, type, number params
        LOGGER.info(f'{i:>3}{str(f):>18}{n_:>3}{np:10.0f}  {t:<40}{str(args):<30}')  # print
        save.extend(x % i for x in ([f] if isinstance(f, int) else f) if x != -1)  # append to savelist
        layers.append(m_)
        if i == 0:
            ch,ch2 = [],[]
        if i < backbone1depth + backbonedepth:
            ch.append(c2)
            ch2 = ch
        else:
            ch2.append(c22)
    return nn.Sequential(*layers), sorted(save), backbonedepth, backbone1depth


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cfg', type=str, default='yolov5l-dual-difference_DEM.yaml', help='model.yaml')
    parser.add_argument('--batch-size', type=int, default=1, help='total batch size for all GPUs')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--profile', action='store_true', help='profile model speed')
    parser.add_argument('--line-profile', action='store_true', help='profile model speed layer by layer')
    parser.add_argument('--test', action='store_true', help='test all yolo*.yaml')
    opt = parser.parse_args()
    opt.cfg = check_yaml(opt.cfg)  # check YAML
    print_args(vars(opt))
    device = select_device(opt.device)

    # Create model
    im = torch.rand(opt.batch_size, 3, 640, 640).to(device)
    im2 = torch.rand(opt.batch_size, 3, 640, 640).to(device)
    model = Model(opt.cfg).to(device)

    # Options
    if opt.line_profile:  # profile layer by layer
        _ = model(im,im2, profile=True)

    elif opt.profile:  # profile forward-backward
        results = profile(input=im, ops=[model], n=3)

    elif opt.test:  # test all models
        for cfg in Path(ROOT / 'models').rglob('yolo*.yaml'):
            try:
                _ = Model(cfg)
            except Exception as e:
                print(f'Error in {cfg}: {e}')

    else:  # report fused model summary
        model.fuse()
