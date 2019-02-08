#!/usr/bin/python
# -*- encoding: utf-8 -*-


from logger import *
from models.deeplabv3plus import Deeplab_v3plus
from cityscapes import CityScapes
from evaluate import MscEval
from optimizer import Optimizer
from loss import OhemCELoss

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torch.nn.functional as F
import torch.distributed as dist

import os
import logging
import time
import datetime
import argparse


respth = './res'
if not osp.exists(respth): os.makedirs(respth)


def parse_args():
    parse = argparse.ArgumentParser()
    parse.add_argument(
            '--local_rank',
            dest = 'local_rank',
            type = int,
            default = -1,
            )
    return parse.parse_args()


def train(verbose=True, **kwargs):
    args = parse_args()
    torch.cuda.set_device(args.local_rank)
    dist.init_process_group(
                backend = 'nccl',
                init_method = 'tcp://127.0.0.1:32168',
                world_size = torch.cuda.device_count(),
                rank = args.local_rank
                )
    setup_logger(respth)
    logger = logging.getLogger()

    ## dataset
    n_classes = 19
    batchsize = 4
    n_workers = 4
    ds = CityScapes('./data', mode='train', cropsize=(768, 768))
    sampler = torch.utils.data.distributed.DistributedSampler(ds)
    dl = DataLoader(ds,
                    batch_size = batchsize,
                    shuffle = False,
                    sampler = sampler,
                    num_workers = n_workers,
                    pin_memory = True,
                    drop_last = True)

    ## model
    ignore_idx = 255
    net = Deeplab_v3plus(n_classes=n_classes)
    net.train()
    net.cuda()
    net = nn.parallel.DistributedDataParallel(net,
            device_ids = [args.local_rank, ],
            output_device = args.local_rank
            )
    criteria = OhemCELoss(thresh=0.7, n_min=batchsize*768*768//16).cuda()

    ## optimizer
    momentum = 0.9
    weight_decay = 5e-4
    lr_start = 1e-2
    power = 0.9
    warmup_steps = 1000
    warmup_start_lr = 5e-6
    max_iter = 41000
    optim = Optimizer(
            net,
            lr_start,
            momentum,
            weight_decay,
            warmup_steps,
            warmup_start_lr,
            max_iter,
            power
            )

    ## train loop
    msg_iter = 50
    eval_iter = 190000
    loss_avg = []
    st = glob_st = time.time()
    diter = iter(dl)
    n_epoch = 0
    for it in range(max_iter):
        try:
            im, lb = next(diter)
            if not im.size()[0]==batchsize: continue
        except StopIteration:
            n_epoch += 1
            sampler.set_epoch(n_epoch)
            diter = iter(dl)
            im, lb = next(diter)
        im = im.cuda()
        lb = lb.cuda()

        H, W = im.size()[2:]
        lb = torch.squeeze(lb, 1)

        optim.zero_grad()
        logits = net(im)
        loss = criteria(logits, lb)
        loss.backward()
        optim.step()

        loss_avg.append(loss.item())
        ## print training log message
        if it%msg_iter==0 and not it==0:
            loss_avg = sum(loss_avg) / len(loss_avg)
            lr = optim.lr
            ed = time.time()
            t_intv, glob_t_intv = ed - st, ed - glob_st
            eta = int((max_iter - it) * (glob_t_intv / it))
            eta = str(datetime.timedelta(seconds = eta))
            msg = ', '.join([
                    'it: {it}/{max_it}',
                    'lr: {lr:4f}',
                    'loss: {loss:.4f}',
                    'eta: {eta}',
                    'time: {time:.4f}',
                ]).format(
                    it = it,
                    max_it = max_iter,
                    lr = lr,
                    loss = loss_avg,
                    time = t_intv,
                    eta = eta
                )
            logger.info(msg)
            loss_avg = []
            st = ed

    ## dump the final model and evaluate the result
    if verbose:
        save_pth = osp.join(respth, 'model_final.pth')
        net.cpu()
        state = net.module.state_dict() if hasattr(net, 'module') else net.state_dict()
        torch.save(state, save_pth)
        logger.info('training done, model saved to: {}'.format(save_pth))
        logger.info('evaluating the final model')
        net.cuda()
        net.eval()
        evaluator = MscEval()
        mIOU = evaluator(net)
        logger.info('mIOU is: {}'.format(mIOU))


if __name__ == "__main__":
    train()
