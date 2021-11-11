import logging
import math
import os

import torch
from torch import nn
import torch.nn.functional as F
from torch.nn.modules.loss import _Loss

# from bert import BertModel, BertOnlyMLMHead
from transformers.models.bert.modeling_bert import BertModel, BertOnlyMLMHead
# from transformers.modeling_bert import BertOnlyMLMHead


# BERTModel.forward extend_attention_mask [batch_size, from_seq_length, to_seq_length]

def select_worst_as_mask(token_probs, num_mask):
    bsz, seq_len = token_probs.size()
    masks = [token_probs[batch, :].topk(max(1, num_mask[batch]), largest=False, sorted=False)[1] for batch in range(bsz)]
    masks = [torch.cat([mask, mask.new(seq_len - mask.size(0)).fill_(mask[0])], dim=0) for mask in masks]
    return torch.stack(masks, dim=0)

def assign_single_value_long(x, i, y):
    b, l = x.size()
    i = i + torch.arange(0, b*l, l, device=i.device).unsqueeze(1)
    x.view(-1)[i.view(-1)] = y

class LabelSmoothingLoss(_Loss):
    """
    With label smoothing,
    KL-divergence between q_{smoothed ground truth prob.}(w)
    and p_{prob. computed by model}(w) is minimized.
    """

    def __init__(self, label_smoothing=0, tgt_vocab_size=0, ignore_index=0,
                 size_average=None, reduce=None, reduction='mean'):
        assert 0.0 < label_smoothing <= 1.0
        self.ignore_index = ignore_index
        super(LabelSmoothingLoss, self).__init__(
            size_average=size_average, reduce=reduce, reduction=reduction)

        assert label_smoothing > 0
        assert tgt_vocab_size > 0

        smoothing_value = label_smoothing / (tgt_vocab_size - 2)
        one_hot = torch.full((tgt_vocab_size,), smoothing_value)
        one_hot[self.ignore_index] = 0
        self.register_buffer('one_hot', one_hot.unsqueeze(0))
        self.confidence = 1.0 - label_smoothing
        self.tgt_vocab_size = tgt_vocab_size

    def forward(self, output, target):
        """
        output (FloatTensor): batch_size * num_pos * n_classes
        target (LongTensor): batch_size * num_pos
        """
        assert self.tgt_vocab_size == output.size(2)
        batch_size, num_pos = target.size(0), target.size(1)
        output = output.view(-1, self.tgt_vocab_size)
        target = target.view(-1)
        model_prob = self.one_hot.float().repeat(target.size(0), 1)
        model_prob.scatter_(1, target.unsqueeze(1), self.confidence)
        model_prob.masked_fill_((target == self.ignore_index).unsqueeze(1), 0)

        return F.kl_div(output, model_prob, reduction='none').view(batch_size, num_pos, -1).sum(2)

class NAT(nn.Module):
    def __init__(self, unilm_path, use_glat=False, glat_random_prob=None, glat_f=0.5, label_smoothing=0.1,
                 sep_word_id=102, mask_word_id=103, pad_word_id=0, clear_bert_weight=False,):
        super(NAT, self).__init__()
        self.source_type_id = 0
        self.target_type_id = 1


        self.use_glat = use_glat
        self.glat_random_prob = glat_random_prob
        self.glat_f = glat_f
        self.mask_word_id = mask_word_id
        self.sep_word_id = sep_word_id
        self.pad_word_id = pad_word_id
        self.label_smoothing = label_smoothing

        self.bert = BertModel.from_pretrained(unilm_path)

        if clear_bert_weight:
            self.bert.init_weights()

        self.config = self.bert.config
        self.config.__dict__['label_smoothing'] = label_smoothing
        self.encoder_embed_dim = self.bert.config.hidden_size
        self.embed_length = nn.Embedding(512, self.encoder_embed_dim , None)
        #init cls decoder weight with embedding
        self.cls = BertOnlyMLMHead(self.bert.config)
        self.cls.predictions.decoder.weight = nn.Parameter(self.bert.embeddings.word_embeddings.weight.clone())

        if self.config.label_smoothing > 0:
            self.crit_mask_lm_smoothed = LabelSmoothingLoss(
                self.config.label_smoothing, self.config.vocab_size, ignore_index=0, reduction='none')
            self.crit_mask_lm = None
        else:
            self.crit_mask_lm_smoothed = None
            self.crit_mask_lm = nn.CrossEntropyLoss(reduction='none')

    @staticmethod
    def create_mask_and_position_ids(num_tokens, max_len, offset=None):
        base_position_matrix = torch.arange(
            0, max_len, dtype=num_tokens.dtype, device=num_tokens.device).view(1, -1)
        mask = (base_position_matrix < num_tokens.view(-1, 1)).type_as(num_tokens)
        if offset is not None:
            base_position_matrix = base_position_matrix + offset.view(-1, 1)
        position_ids = base_position_matrix * mask
        return mask, position_ids

    @staticmethod
    def create_attention_mask(source_mask, target_mask):
        b = source_mask.shape[0]
        sl = source_mask.shape[1]
        tl = target_mask.shape[1]
        weight = torch.cat((torch.ones_like(source_mask), torch.zeros_like(target_mask)), dim=1)
        from_weight = weight.unsqueeze(-1)
        to_weight = weight.unsqueeze(1)

        mask = torch.cat((source_mask, target_mask), dim=1) == 1
        mask = mask.unsqueeze(-1) & mask.unsqueeze(1)
        # w[i][j] = f[i][0] == 1 or t[0][j] == 0
        return (((from_weight == 0) | (to_weight == 1)) & mask).type_as(source_mask)


    def forward_length(self, enc_feats, src_masks):
        # enc_feats: B x T x C
        # src_masks: B x T or None
        enc_feats = enc_feats.transpose(0, 1)
        src_masks = src_masks.transpose(0, 1)
        #src_masks = (~src_masks).type_as(enc_feats)
        src_masks = src_masks.type_as(enc_feats)
        enc_feats = (
            (enc_feats / src_masks.sum(0)[None, :, None]) * src_masks[:, :, None]
        ).sum(0)
        length_out = F.linear(enc_feats, self.embed_length.weight)
        return F.log_softmax(length_out, -1)


    def feed_bert(self, input_ids, source_mask, target_mask,
                  token_type_ids, position_ids, target_position_ids,
                  target_ids=None, decoding=False):

        attention_mask = self.create_attention_mask(source_mask, target_mask)
        decoder_relative_position_mask = None
        source_len = source_mask.size(1)

        outputs = self.bert(input_ids=input_ids, attention_mask=attention_mask,
                            token_type_ids=token_type_ids,
                            output_hidden_states=False,
                            position_ids=position_ids)
        sequence_output = outputs[0]
        return sequence_output

    def forward(self, source_ids, target_ids, pseudo_ids, num_source_tokens, num_target_tokens, decode=False):
        if decode:
            source_mask = source_ids != self.pad_word_id
            position_ids = torch.arange(source_ids.shape[1]).repeat(source_ids.shape[0], 1).to(source_ids.device)
            position_ids.masked_fill_(~source_mask, 0)
            token_type_ids = torch.zeros_like(source_ids).to(source_ids.device)
            token_type_ids.masked_fill_(~source_mask, 1)

            length_out = (target_ids != self.pad_word_id).sum(-1)
            prediction_tokens, pred_length_out = self.forward_decode(source_ids, token_type_ids,
                                                                    position_ids, source_mask)
            len_acc = (length_out == pred_length_out).sum()
            min_size = min(target_ids.shape[-1], prediction_tokens.shape[-1])
            _target_mask = (target_ids != self.pad_word_id)[:, :min_size]
            tokens_acc = (prediction_tokens[:, :min_size] == target_ids[:, :min_size]).masked_fill(~_target_mask, 0).sum()
            tokens_acc = torch.true_divide(tokens_acc, _target_mask.sum())
            len_acc = torch.true_divide(len_acc, length_out.shape[0])
            return prediction_tokens, pred_length_out, len_acc, tokens_acc

        if self.use_glat:
            with torch.no_grad():
                pseudo_ids, N = self.forward_glat(source_ids, target_ids, pseudo_ids, num_source_tokens, num_target_tokens)
        source_len = source_ids.size(1)
        target_len = target_ids.size(1)
        pseudo_len = pseudo_ids.size(1)
        assert target_len == pseudo_len
        assert source_len > 0 and target_len > 0

        input_ids = torch.cat((source_ids, pseudo_ids), dim=1)
        token_type_ids = torch.cat(
            (torch.ones_like(source_ids) * self.source_type_id,
             torch.ones_like(pseudo_ids) * self.target_type_id), dim=1)

        source_mask, source_position_ids = \
            self.create_mask_and_position_ids(num_source_tokens, source_len)
        target_mask, target_position_ids = \
            self.create_mask_and_position_ids(num_target_tokens, target_len, offset=num_source_tokens)

        position_ids = torch.cat((source_position_ids, target_position_ids), dim=1)

        sequence_output = self.feed_bert(input_ids, source_mask, target_mask,
                                         token_type_ids, position_ids, source_position_ids, target_position_ids)

        length_tgt = target_mask.sum(-1)
        length_out = self.forward_length(sequence_output[:, :source_len], source_mask)
        length_loss = F.cross_entropy(length_out, length_tgt)


        def loss_mask_and_normalize(loss, mask):
            mask = mask.type_as(loss)
            loss = loss * mask
            denominator = torch.sum(mask) + 1e-5
            return (loss / denominator).sum()

        pseudo_sequence_output = sequence_output[:, source_len:, ]
        prediction_scores_masked = self.cls(pseudo_sequence_output)
        if self.crit_mask_lm_smoothed:
            masked_lm_loss = self.crit_mask_lm_smoothed(
                F.log_softmax(prediction_scores_masked.float(), dim=-1), target_ids)
        else:
            masked_lm_loss = self.crit_mask_lm(
                prediction_scores_masked.transpose(1, 2).float(), target_ids)
        pseudo_lm_loss = loss_mask_and_normalize(masked_lm_loss.float(), target_mask)
        if self.use_glat:
            return pseudo_lm_loss, length_loss, torch.mean(N.float())
        else:
            return pseudo_lm_loss, length_loss

    def forward_glat(self, source_ids, target_ids, pseudo_ids, num_source_tokens, num_target_tokens):
        source_len = source_ids.size(1)
        target_len = target_ids.size(1)
        pseudo_len = pseudo_ids.size(1)
        assert target_len == pseudo_len
        assert source_len > 0 and target_len > 0

        input_ids = torch.cat((source_ids, pseudo_ids), dim=1)
        token_type_ids = torch.cat(
            (torch.ones_like(source_ids) * self.source_type_id,
             torch.ones_like(pseudo_ids) * self.target_type_id), dim=1)

        source_mask, source_position_ids = \
            self.create_mask_and_position_ids(num_source_tokens, source_len)
        target_mask, target_position_ids = \
            self.create_mask_and_position_ids(num_target_tokens, target_len, offset=num_source_tokens)

        #pseudo_ids.scatter_(1, (target_mask.sum(-1) - 1).view(-1, 1), self.sep_word_id)

        position_ids = torch.cat((source_position_ids, target_position_ids), dim=1)

        sequence_output = self.feed_bert(input_ids, source_mask, target_mask,
                                         token_type_ids, position_ids, source_position_ids, target_position_ids)


        # pseudo_sequence_output = sequence_output[:, source_len:, ]

        pseudo_sequence_output = sequence_output[:, source_len:, ]
        prediction_scores_masked = self.cls(pseudo_sequence_output)
        prediction_tokens = prediction_scores_masked.max(-1)[-1]
        N = ((prediction_tokens != target_ids) & (target_mask == 1)).sum(-1) * self.glat_f
        N = N.long()

        _, indices = torch.sort(torch.rand(pseudo_ids.shape), descending=True)
        indices = indices.to(source_ids.device)
        if self.glat_random_prob:
            ind_masks = torch.rand_like(indices.float()) > self.glat_random_prob
            ind_masks.to(indices.device)
        for i, indice in enumerate(indices):
            indice = indice[indice < target_mask[i].sum()]
            n = N[i].item()
            if self.glat_random_prob:
                ind = indice[:n]
                ind_mask = ind_masks[i][:n]
                pseudo_ids[i, ind[ind_mask]]  = target_ids[i, ind[ind_mask]]
                rn = (n - ind_mask.sum()).item()
                if rn > 0:
                    pseudo_ids[i, ind[~ind_mask]]  = torch.randint(0, self.config.vocab_size-1, (rn,)).long().to(ind.device)
            else:
                pseudo_ids[i, indice[:n]] = target_ids[i, indice[:n]]

        return pseudo_ids, N

    def forward_decode(self, input_ids, token_type_ids, position_ids, input_mask, length_out=None):
        source_len = input_ids.shape[1]
        token_type_ids = token_type_ids[:, :source_len]
        position_ids = position_ids[:, :source_len]
        input_mask = input_mask[:, :source_len]
        source_ids = input_ids
        source_mask, source_position_ids = (input_ids != self.pad_word_id).int(), position_ids

        if length_out is None:
            weight = torch.ones_like(input_ids)
            weight[input_ids == self.pad_word_id] = 0
            from_weight = weight.unsqueeze(-1)
            to_weight = weight.unsqueeze(1)
            attention_mask = ((from_weight > 0) & (to_weight > 0)).bool()

            outputs = self.bert(input_ids, token_type_ids=token_type_ids, attention_mask=attention_mask,
                                        position_ids=position_ids, output_hidden_states=False)

            sequence_output = outputs['last_hidden_state']

            length_out = self.forward_length(sequence_output, source_mask)
            length_out = length_out.max(-1)[1]
            length_out[length_out > 48] = 48
            length_out[length_out < 7] = 7
        else:
            length_out += 1

        target_len = length_out.max().item()
        if target_len + source_len > 512:
            source_len = 512-target_len
            source_ids = source_ids[:, :source_len]
            source_position_ids = source_position_ids[:, :source_len]
            source_mask = source_mask[:, :source_len]

        pseudo_ids = torch.empty(length_out.shape[0], target_len).fill_(self.mask_word_id).to(input_ids.device)
        base_position_matrix = torch.arange(target_len, dtype=input_ids.dtype,
                                            device=input_ids.device).view(1, -1)

        pseudo_mask = base_position_matrix < length_out.view(-1, 1)
        #pseudo_ids.scatter_(1, (pseudo_mask.sum(-1) - 1).view(-1, 1), self.sep_word_id)

        pseudo_position_ids = base_position_matrix * pseudo_mask + source_mask.sum(-1).view(-1, 1)

        pseudo_ids[~pseudo_mask] = self.pad_word_id
        input_ids = torch.cat((source_ids, pseudo_ids), dim=1).long()

        position_ids = torch.cat((source_position_ids, pseudo_position_ids), dim=1)
        token_type_ids = torch.cat(
            (torch.ones_like(source_ids) * self.source_type_id,
             torch.ones_like(pseudo_ids) * self.target_type_id), dim=1).long()

        sequence_output = self.feed_bert(input_ids, source_mask, pseudo_mask,
                                token_type_ids, position_ids, pseudo_position_ids,
                                decoding=True)

        prediction_scores = self.cls(sequence_output[:, source_len:, ])
        prediction_tokens = prediction_scores.max(-1)[-1]
        prediction_tokens[~pseudo_mask] = self.pad_word_id
        return prediction_tokens, length_out
