import os
import sys
import math
import rdkit
import random
import argparse
import numpy as np
import cPickle as pickle

import torch
import torch.nn as nn
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler

from collections import deque
from torch.autograd import Variable
from torch.utils.data import DataLoader
from jtvae import JTNNVAE, Vocab, MolTreeFolder


torch.cuda.set_device(0)

lg = rdkit.RDLogger.logger()
lg.setLevel(rdkit.RDLogger.CRITICAL)

parser = argparse.ArgumentParser()
parser.add_argument('--train', required=True)
parser.add_argument('--vocab', required=True)
parser.add_argument('--save_dir', required=True)
parser.add_argument('--load_epoch', type=int, default=0)

parser.add_argument('--hidden_size', type=int, default=450)
parser.add_argument("--hierarchical", action="store_true", default=False)
parser.add_argument('--batch_size', type=int, default=10)
parser.add_argument('--tree_latent_size', type=int, default=56)
parser.add_argument('--mol_latent_size', type=int, default=56)
parser.add_argument('--depthT', type=int, default=20)
parser.add_argument('--depthG', type=int, default=3)

parser.add_argument('--lr', type=float, default=1e-3)
parser.add_argument('--clip_norm', type=float, default=50.0)
parser.add_argument('--beta', type=float, default=0.0)
parser.add_argument('--step_beta', type=float, default=0.001)
parser.add_argument('--max_beta', type=float, default=1.0)
parser.add_argument('--warmup', type=int, default=5000)

parser.add_argument('--epoch', type=int, default=20)
parser.add_argument('--anneal_rate', type=float, default=0.9)
parser.add_argument('--anneal_iter', type=int, default=40000)
parser.add_argument('--kl_anneal_iter', type=int, default=1000)
parser.add_argument('--print_iter', type=int, default=50)
parser.add_argument('--save_iter', type=int, default=5000)

args = parser.parse_args()
print args

vocab = [x.strip("\r\n ") for x in open(args.vocab)]
vocab = Vocab(vocab)

model = JTNNVAE(vocab, args.hierarchical, args.hidden_size, args.tree_latent_size,
                args.mol_latent_size, args.depthT, args.depthG).cuda()
print model

for param in model.parameters():
    if param.dim() == 1:
        nn.init.constant_(param, 0)
    else:
        nn.init.xavier_normal_(param)

if args.load_epoch > 0:
    model.load_state_dict(torch.load(
        args.save_dir + "/model.iter-" + str(args.load_epoch)
    ))

print "Model #Params: %dK" % (
    sum([x.nelement() for x in model.parameters()]) / 1000,
)

optimizer = optim.Adam(model.parameters(), lr=args.lr)
scheduler = lr_scheduler.ExponentialLR(optimizer, args.anneal_rate)
scheduler.step()


def param_norm(m):
    return math.sqrt(
        sum([p.norm().item() ** 2
             for p in m.parameters()])
    )


def grad_norm(m):
    return math.sqrt(
        sum([p.grad.norm().item() ** 2
             for p in m.parameters()
             if p.grad is not None
             ])
    )


if not os.path.exists(args.save_dir):
    os.makedirs(args.save_dir)

total_step = args.load_epoch
beta = args.beta
meters = np.zeros(5)

for epoch in xrange(args.epoch):
    loader = MolTreeFolder(args.train, vocab, args.batch_size, num_workers=4)
    for batch in loader:
        total_step += 1
        try:
            model.zero_grad()
            loss, recon_loss, kl_div, wacc, tacc, sacc = model(
                batch, beta, total_step > args.warmup
            )
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), args.clip_norm)
            optimizer.step()
        except Exception as e:
            print e
            continue

        meters = meters + \
            np.array([recon_loss, kl_div, wacc * 100, tacc * 100, sacc * 100])

        if total_step % args.print_iter == 0:
            meters /= args.print_iter

            # print "[%d] Beta: %.3f, RL: %.3f, KL: %.3f, Word: %.2f, Topo: %.2f, Assm: %.2f, PNorm: %.2f, GNorm: %.2f" % \
            #     tuple([total_step, beta] + list(meters) +
            #           [param_norm(model), grad_norm(model)])
            print "[%d] Beta: %.3f, RL: %.3f, KL: %.3f, Word: %.2f, Topo: %.2f, Assm: %.2f" % \
                  tuple([total_step, beta] + list(meters))
            sys.stdout.flush()

            meters *= 0

        if total_step % args.save_iter == 0:
            torch.save(model.state_dict(), args.save_dir +
                       "/model.iter-" + str(total_step))

        if total_step % args.anneal_iter == 0:
            scheduler.step()
            print "learning rate: %.6f" % scheduler.get_lr()[0]

        if total_step % args.kl_anneal_iter == 0 and total_step >= args.warmup:
            beta = min(args.max_beta, beta + args.step_beta)
