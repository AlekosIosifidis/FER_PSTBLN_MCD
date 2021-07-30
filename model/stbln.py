"""
Modified based on: https://github.com/open-mmlab/mmskeleton
"""

import math
import numpy as np
import torch
import torch.nn as nn


def import_class(name):
    components = name.split('.')
    mod = __import__(components[0])
    for comp in components[1:]:
        mod = getattr(mod, comp)
    return mod


def weights_init(module_, bs=1):
    if isinstance(module_, nn.Conv2d) and bs == 1:
        nn.init.kaiming_normal_(module_.weight, mode='fan_out')
        nn.init.constant_(module_.bias, 0)
    elif isinstance(module_, nn.Conv2d) and bs != 1:
        nn.init.normal_(module_.weight, 0,
                        math.sqrt(2. / (module_.weight.size(0) * module_.weight.size(1) * module_.weight.size(2) * bs)))
        nn.init.constant_(module_.bias, 0)
    elif isinstance(module_, nn.BatchNorm2d):
        nn.init.constant_(module_.weight, bs)
        nn.init.constant_(module_.bias, 0)
    elif isinstance(module_, nn.Linear):
        nn.init.normal_(module_.weight, 0, math.sqrt(2. / bs))


class BilinearMapping(nn.Module):
    def __init__(self, in_channels, out_channels, graph_dim1, graph_dim2):
        super(BilinearMapping, self).__init__()
        self.num_subset = 3
        self.rand_graph = nn.Parameter(torch.from_numpy(
            np.random.rand(self.num_subset, graph_dim1, graph_dim2).astype(np.float32)))
        self.g_conv = nn.ModuleList()
        for i in range(self.num_subset):
            self.g_conv.append(nn.Conv2d(in_channels, out_channels, 1))
            weights_init(self.g_conv[i], bs=self.num_subset)

        if graph_dim1 != graph_dim2:
            self.gcn_residual = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, kernel_size=(graph_dim2, graph_dim1)),
                nn.BatchNorm2d(out_channels)
            )
            weights_init(self.gcn_residual[0], bs=1)
            weights_init(self.gcn_residual[1], bs=1)
        else:
            self.gcn_residual = nn.Sequential(
                    nn.Conv2d(in_channels, out_channels, kernel_size=(1, 1)),
                    nn.BatchNorm2d(out_channels)
            )

        self.bn = nn.BatchNorm2d(out_channels)
        weights_init(self.bn, bs=1e-6)
        self.relu = nn.ReLU()

    def forward(self, x):
        N, C, T, V = x.size()
        A = self.rand_graph
        A = A.cuda(x.get_device())
        hidden_ = None
        for i in range(self.num_subset):
            x_a = x.view(N, C * T, V)
            z = self.g_conv[i](torch.matmul(x_a, A[i]).view(N, C, T, A.size(2)))
            hidden_ = z + hidden_ if hidden_ is not None else z
        hidden_ = self.bn(hidden_)
        hidden_ += self.gcn_residual(x)
        return self.relu(hidden_)


class TemporalConvolution(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size_joint=1, kernel_size_=9, stride=1):
        super(TemporalConvolution, self).__init__()

        pad = int((kernel_size_ - 1) / 2)
        self.t_conv = nn.Conv2d(in_channels, out_channels, kernel_size=(kernel_size_, kernel_size_joint),
                                padding=(pad, 0), stride=(stride, 1))
        self.bn = nn.BatchNorm2d(out_channels)
        weights_init(self.t_conv, bs=1)
        weights_init(self.bn, bs=1)

    def forward(self, x):
        x = self.bn(self.t_conv(x))
        return x


class ST_BLN_block(nn.Module):
    def __init__(self, in_channels, out_channels, graph_dim1=25, graph_dim2=25, stride=1, residual=True):
        super(ST_BLN_block, self).__init__()

        self.gcn = BilinearMapping(in_channels, out_channels, graph_dim1, graph_dim2)
        self.tcn = TemporalConvolution(out_channels, out_channels, stride=stride)
        self.relu = nn.ReLU()
        if not residual:
            self.residual = lambda x: 0
        else:
            if graph_dim1 != graph_dim2:
                self.residual = TemporalConvolution(in_channels, out_channels, kernel_size_joint=graph_dim1,
                                                    stride=stride)
            else:
                self.residual = TemporalConvolution(in_channels, out_channels, kernel_size_joint=1, stride=stride)

    def forward(self, x):
        x = self.tcn(self.gcn(x)) + self.residual(x)
        return self.relu(x)


class STBLN(nn.Module):
    def __init__(self, num_class=6, num_point=51, num_person=1, in_channels=2):
        super(STBLN, self).__init__()

        self.data_bn = nn.BatchNorm1d(num_person * in_channels * num_point)
        weights_init(self.data_bn, bs=1)
        self.dropout_ = nn.Dropout(p=0.2)
        self.layers = nn.ModuleDict(
            {'layer1': ST_BLN_block(2, 8, num_point, num_point, residual=False),
             'layer2': ST_BLN_block(8, 16, num_point, num_point),
             'layer3': ST_BLN_block(16, 16, num_point, num_point),
             'layer4': ST_BLN_block(16, 32, num_point, num_point),
             'layer5': ST_BLN_block(32, 32, num_point, num_point),
             'layer6': ST_BLN_block(32, 64, num_point, num_point),
             'layer7': ST_BLN_block(64, 64, num_point, num_point)}
        )

        self.fc = nn.Linear(64, num_class)
        weights_init(self.fc, bs=num_class)

    def forward(self, x):
        N, C, T, V, M = x.size()
        x = x.permute(0, 4, 3, 1, 2).contiguous().view(N, M * V * C, T)
        x = self.data_bn(x)
        x = x.view(N, M, V, C, T).permute(0, 1, 3, 4, 2).contiguous().view(N * M, C, T, V)
        for i in range(len(self.layers)):
            x = self.dropout_(x)
            x = self.layers['layer' + str(i+1)](x)
        # N*M,C,T,V
        c_new = x.size(1)
        x = x.view(N, M, c_new, -1)
        x = x.mean(3).mean(1)
        return self.fc(x)
