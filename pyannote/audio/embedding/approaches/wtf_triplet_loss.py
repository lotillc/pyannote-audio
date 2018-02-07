#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2018 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr

import numpy as np
from tqdm import tqdm
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
from pyannote.audio.generators.speaker import SpeechSegmentGenerator
from pyannote.audio.callback import LoggingCallbackPytorch
from torch.optim import Adam
from scipy.spatial.distance import pdist
from .triplet_loss import TripletLoss
from pyannote.metrics.binary_classification import det_curve


class WTFTripletLoss(TripletLoss):
    """

    Parameters
    ----------
    variant : int, optional
        Loss variants. Defaults to 1.
    duration : float, optional
        Defautls to 3.2 seconds.
    margin: float, optional
        Margin factor. Defaults to 0.2.
    sampling : {'all', 'hard', 'negative'}, optional
        Triplet sampling strategy.
    per_label : int, optional
        Number of sequences per speaker in each batch. Defaults to 3.
    per_fold : int, optional
        If provided, sample triplets from groups of `per_fold` speakers at a
        time. Defaults to sample triplets from the whole speaker set.
    trim : float, optional
        Do not use speech segments that are that close to the beginning/end of
        the annotated region. Useful when features are short-term normalized,
        as this can result in weird feature distribution at the boundaries.
        Defaults to 0s (i.e. do no trim).
    parallel : int, optional
        Number of prefetching background generators. Defaults to 1.
        Each generator will prefetch enough batches to cover a whole epoch.
        Set `parallel` to 0 to not use background generators.
    """

    def __init__(self, variant=1, duration=3.2, sampling='all',
                 per_label=3, per_fold=None, trim=0, parallel=1):

        super(WTFTripletLoss, self).__init__(
            duration=duration, metric='angular', clamp='sigmoid',
            sampling=sampling, per_label=per_label, per_fold=per_fold,
            trim=trim, parallel=parallel)

        self.variant = variant


    def fit(self, model, feature_extraction, protocol, log_dir, subset='train',
            epochs=1000, restart=None, gpu=False):

        import tensorboardX
        writer = tensorboardX.SummaryWriter(log_dir=log_dir)

        logging_callback = LoggingCallbackPytorch(
            log_dir=log_dir, restart=(False if restart is None else True))

        batch_generator = SpeechSegmentGenerator(
            feature_extraction,
            per_label=self.per_label, per_fold=self.per_fold,
            duration=self.duration, trim=self.trim, parallel=self.parallel)
        batches = batch_generator(protocol, subset=subset)
        batch = next(batches)

        batches_per_epoch = batch_generator.batches_per_epoch

        if restart is not None:
            weights_pt = logging_callback.WEIGHTS_PT.format(
                log_dir=log_dir, epoch=restart)
            model.load_state_dict(torch.load(weights_pt))

        if gpu:
            model = model.cuda()

        model.internal = False

        if self.variant == 2:

            # rolling estimate of embedding norm distribution
            self.norm_batch_norm_ = nn.BatchNorm1d(1, eps=1e-5,
                                                   momentum=0.1,
                                                   affine=False)

        optimizer = Adam(model.parameters())
        if restart is not None:
            optimizer_pt = logging_callback.OPTIMIZER_PT.format(
                log_dir=log_dir, epoch=restart)
            optimizer.load_state_dict(torch.load(optimizer_pt))
            if gpu:
                for state in optimizer.state.values():
                    for k, v in state.items():
                        if torch.is_tensor(v):
                            state[k] = v.cuda()

        restart = 0 if restart is None else restart + 1
        for epoch in range(restart, restart + epochs):

            tloss_avg, closs_avg = 0., 0.

            if epoch % 10 == 0:
                positive, negative = [], []
                norms = []

            desc = 'Epoch #{0}'.format(epoch)
            for i in tqdm(range(batches_per_epoch), desc=desc):

                model.zero_grad()

                batch = next(batches)

                X = Variable(torch.from_numpy(
                    np.array(np.rollaxis(batch['X'], 0, 2),
                             dtype=np.float32)))

                if gpu:
                    X = X.cuda()

                fX = model(X)

                if epoch % 10 == 0:
                    if gpu:
                        fX_ = fX.data.cpu().numpy()
                    else:
                        fX_ = fX.data.numpy()
                    norms.append(np.linalg.norm(fX_, axis=1))

                # pre-compute pairwise distances
                distances = self.pdist(fX)

                if epoch % 10 == 0:
                    if gpu:
                        distances_ = distances.data.cpu().numpy()
                    else:
                        distances_ = distances.data.numpy()
                    is_positive = pdist(batch['y'].reshape((-1, 1)), metric='chebyshev') < 1
                    positive.append(distances_[np.where(is_positive)])
                    negative.append(distances_[np.where(~is_positive)])

                # sample triplets
                triplets = getattr(self, 'batch_{0}'.format(self.sampling))
                anchors, positives, negatives = triplets(batch['y'], distances)

                # compute triplet loss
                tlosses, deltas = self.triplet_loss(
                    distances, anchors, positives, negatives,
                    return_delta=True)

                tloss = torch.mean(tlosses)

                if self.variant == 1:
                    # compute wtf loss
                    closses = F.sigmoid(
                        F.softsign(deltas) * torch.norm(fX[anchors], 2, 1, keepdim=True))

                elif self.variant == 2:

                    anchor_norms = torch.norm(fX[anchors], 2, 1, keepdim=True)
                    anchor_norms = self.norm_batch_norm_(anchor_norms)
                    confidence = F.sigmoid(anchor_norms)

                    correctness = 2 * F.sigmoid(F.relu(-deltas)) - 1

                    closses = torch.abs(confidence - correctness)

                closs = torch.mean(closses)

                # log loss
                if gpu:
                    tloss_ = float(tloss.data.cpu().numpy())
                    closs_ = float(closs.data.cpu().numpy())
                else:
                    tloss_ = float(tloss.data.numpy())
                    closs_ = float(closs.data.numpy())
                tloss_avg += tloss_
                closs_avg += closs_

                loss = tloss + closs
                loss.backward()
                optimizer.step()

            tloss_avg /= batches_per_epoch
            writer.add_scalar('tloss', tloss_avg, global_step=epoch)

            closs_avg /= batches_per_epoch
            writer.add_scalar('closs', closs_avg, global_step=epoch)

            if epoch % 10 == 0:

                positive = np.hstack(positive)
                negative = np.hstack(negative)
                writer.add_histogram(
                    'embedding/pairwise_distance/positive', positive,
                    global_step=epoch, bins='auto')
                writer.add_histogram(
                    'embedding/pairwise_distance/negative', negative,
                    global_step=epoch, bins='auto')

                _, _, _, eer = det_curve(
                    np.hstack([np.ones(len(positive)), np.zeros(len(negative))]),
                    np.hstack([positive, negative]), distances=True)
                writer.add_scalar('eer', eer, global_step=epoch)

                norms = np.hstack(norms)
                writer.add_histogram(
                    'embedding/norm', norms,
                    global_step=epoch, bins='auto')

            logging_callback.model = model
            logging_callback.optimizer = optimizer
            logging_callback.on_epoch_end(epoch)