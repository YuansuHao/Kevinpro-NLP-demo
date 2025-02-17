from __future__ import unicode_literals, print_function, division
import os
import time
import argparse
import torch
from model import Model
from torch.nn.utils import clip_grad_norm_
from torch.optim import Adagrad
from torch.utils.data import Dataset, DataLoader
import numpy as np
from EasyTransformer.util import ProgressBar
import warnings

import config
from dataloader import Tokenizer, SumDataset

warnings.filterwarnings('ignore')
use_cuda = config.use_gpu and torch.cuda.is_available()


class Train(object):
    def __init__(self):
        self.tokenizer = Tokenizer()
        self.dataset = SumDataset()
        self.dataloader = DataLoader(self.dataset, batch_size=config.batch_size, shuffle=True, drop_last=True)

        train_dir = os.path.join(config.save_path, 'train_%d' % (int(time.time())))
        if not os.path.exists(train_dir):
            os.mkdir(train_dir)

        self.model_dir = os.path.join(train_dir, 'model')
        if not os.path.exists(self.model_dir):
            os.mkdir(self.model_dir)
        self.setup_train()

    def save_model(self, iter):
        state = {
            'iter': iter,
            'encoder_state_dict': self.model.encoder.state_dict(),
            'decoder_state_dict': self.model.decoder.state_dict(),
            'reduce_state_dict': self.model.reduce_state.state_dict(),
            'optimizer': self.optimizer.state_dict()
        }
        model_save_path = os.path.join(self.model_dir, 'model_%d_%d' % (iter, int(time.time())))
        torch.save(state, model_save_path)

    def setup_train(self, model_file_path=None):
        self.model = Model(model_file_path)
        params = list(self.model.encoder.parameters()) + list(self.model.decoder.parameters()) + \
            list(self.model.reduce_state.parameters())
        initial_lr = config.lr_coverage if config.is_coverage else config.lr
        self.optimizer = Adagrad(params, lr=initial_lr, initial_accumulator_value=config.adagrad_init_acc)

        start_iter, start_loss = 0, 0

        if model_file_path is not None:
            state = torch.load(model_file_path, map_location=lambda storage, location: storage)
            start_iter = state['iter']
            start_loss = state['current_loss']

            if not config.is_coverage:
                self.optimizer.load_state_dict(state['optimizer'])
                if use_cuda:
                    for state in self.optimizer.state.values():
                        for k, v in state.items():
                            if torch.is_tensor(v):
                                state[k] = v.cuda()

        return start_iter, start_loss

    def train_one_batch(self, enc_batch, dec_batch):
        enc_batch, enc_padding_mask, enc_lens, enc_batch_extend_vocab, extra_zeros, c_t_1, coverage = \
            enc_batch
        dec_batch, dec_padding_mask, max_dec_len, dec_lens_var, target_batch = \
            dec_batch

        self.optimizer.zero_grad()

        encoder_outputs, encoder_feature, encoder_hidden = self.model.encoder(enc_batch, enc_lens)
        s_t_1 = self.model.reduce_state(encoder_hidden)

        step_losses = []
        #print(self.model.decoder.state_dict()['out1.bias'])
        for di in range(min(max_dec_len, config.max_dec_steps)):
            y_t_1 = dec_batch[:, di]  # Teacher forcing
            final_dist, s_t_1,  c_t_1, attn_dist, p_gen, next_coverage = self.model.decoder(y_t_1, s_t_1,
                                                                                            encoder_outputs, encoder_feature, enc_padding_mask, c_t_1,
                                                                                            extra_zeros, enc_batch_extend_vocab,
                                                                                            coverage, di)
            target = target_batch[:, di]
            gold_probs = torch.gather(final_dist, 1, target.unsqueeze(1)).squeeze()
            step_loss = -torch.log(gold_probs + config.eps)
            if config.is_coverage:
                step_coverage_loss = torch.sum(torch.min(attn_dist, coverage), 1)
                step_loss = step_loss + config.cov_loss_wt * step_coverage_loss
                coverage = next_coverage

            step_mask = dec_padding_mask[:, di]
            step_loss = step_loss * step_mask
            step_losses.append(step_loss)

        sum_losses = torch.sum(torch.stack(step_losses, 1), 1)
        batch_avg_loss = sum_losses/dec_lens_var
        loss = torch.mean(batch_avg_loss)
        
        loss.backward()
        
        clip_grad_norm_(self.model.encoder.parameters(), config.max_grad_norm)
        clip_grad_norm_(self.model.decoder.parameters(), config.max_grad_norm)
        clip_grad_norm_(self.model.reduce_state.parameters(), config.max_grad_norm)

        self.optimizer.step()
        return loss.item()

    def train(self,model_file_path=None):
        
        for epoch in range(config.EPOCH):
            #print(self.model.encoder.state_dict()['W_h.weight'])
            print("\nstarting Epoch {}".format(epoch))
            iter = 0
            # for name, parameters in self.model.decoder.named_parameters():
            #     print(name, ':', parameters.size())
            # for parameters in self.model.encoder.parameters():
            #     print(parameters)
            #print(self.model.decoder.state_dict()['out1.bias'])
            total_loss = 0
            pbar = ProgressBar(n_total=len(self.dataloader), desc='Training')
            for enc_input, enc_input_ext, dec_input, dec_output, enc_len, dec_len, oov_word_num in self.dataloader:
                # computing Mask of input and output
                enc_padding_mask = torch.zeros((config.batch_size, config.max_enc_steps))
                for i in range(len(enc_len)):
                    for j in range(enc_len[i]):
                        enc_padding_mask[i][j] = 1

                dec_padding_mask = torch.zeros((config.batch_size, config.max_dec_steps))
                for i in range(len(dec_len)):
                    for j in range(dec_len[i]):
                        dec_padding_mask[i][j] = 1

                max_oov_num = max(oov_word_num).numpy()

                # Packup input and output data to Match the origin API
                enc_batch = enc_input
                extra_zeros = None
                if max_oov_num > 0:
                    extra_zeros = torch.zeros((config.batch_size, max_oov_num))

                c_t_1 = torch.zeros((config.batch_size, 2 * config.hidden_dim))
                coverage = torch.zeros(enc_batch.size())

                dec_batch = dec_input
                target_batch = dec_output
                max_dec_len = max(dec_len).numpy()

                if use_cuda:
                    enc_batch = enc_batch.cuda()
                    enc_padding_mask = enc_padding_mask.cuda()
                    enc_len = enc_len.int()
                    enc_input_ext = enc_input_ext.cuda()
                    if extra_zeros is not None:
                        extra_zeros = extra_zeros.cuda()
                    c_t_1 = c_t_1.cuda()
                    coverage = coverage.cuda()

                    dec_batch = dec_batch.cuda()
                    dec_padding_mask = dec_padding_mask.cuda()
                    #max_dec_len = max_dec_len.cuda()
                    dec_len = dec_len.cuda()
                    target_batch = target_batch.cuda()

                # Pack data for training
                enc_batch_pack = (enc_batch, enc_padding_mask, enc_len, enc_input_ext, extra_zeros, c_t_1, coverage)
                dec_batch_pack = (dec_batch, dec_padding_mask, max_dec_len, dec_len, target_batch)

                # Training
                loss = self.train_one_batch(enc_batch_pack, dec_batch_pack)
                total_loss += loss
                iter += 1

                pbar(iter, {'loss': total_loss/iter})
                
                
            self.save_model(0)

            
            #exit()
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Train script")
    parser.add_argument("-m",
                        dest="model_file_path",
                        required=False,
                        default=None,
                        help="Model file for retraining (default: None).")
    args = parser.parse_args()

    train_processor = Train()
    train_processor.train()
