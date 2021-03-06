from .mask_mle_task import MaskMLETask
from fairseq.tasks import register_task
from fairseq import utils
from fairseq import optim
import torch

from fairseq.criterions import FairseqCriterion, register_criterion
from fairseq.sequence_generator import SequenceGenerator

from tasks.mask_discriminator_task import MaskDiscriminatorTask
from criterions.discriminator_loss import  DiscriminatorCriterion
from copy import deepcopy


@register_task("mask_gan")
class MaskGANTask(MaskMLETask):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.sequence_generator = SequenceGenerator(self.target_dictionary,
                                                    beam_size=1)

        self.discriminator_optimizer = None
        self.discriminator_loss = DiscriminatorCriterion(args[0], self)
        self.discriminator_steps = args[0].discriminator_steps
        self.ignore_mask = args[0].ignore_mask
        self.update_discr_every = args[0].update_discr_every
        self._step_counter = 0
        self.args = args[0]

    @staticmethod
    def add_args(parser):
        super(MaskGANTask, MaskGANTask).add_args(parser)

        parser.add_argument('--discriminator-steps', type=int, default=3)
        parser.add_argument('--ignore-mask', type=bool, default=False)
        parser.add_argument('--update-discr-every', type=int, default=1)

    def process_sample(self, sample, p):
        mask = torch.distributions.Bernoulli(torch.Tensor([p]))
        target = sample['target'].clone()

        mask_tensor = mask.sample(target.size())[:, :, 0].to(target.device)

        pad_idx = self.target_dictionary.pad()
        mask_idx = self.target_dictionary.index("<MASK>")

        target[(target != pad_idx) & (
            mask_tensor.byte())] = mask_idx
        mask_tensor[(target == pad_idx)] = 0

        sample['net_input']['masked_tgt'] = target
        sample['masks'] = mask_tensor
        return sample

    def get_mask_rate(self):
        return 0.8
        #  return torch.clamp(0.1 + self.passed_iters * 0.01, 0., 1.)

    def train_step(self, sample, model, criterion, optimizer,
                   ignore_grad=False):
        """
        Do forward and backward, and return the loss as computed by *criterion*
        for the given *model* and *sample*.

        Args:
            sample (dict): the mini-batch. The format is defined by the
                :class:`~fairseq.data.FairseqDataset`.
            model (~fairseq.models.BaseFairseqModel): the model
            criterion (~fairseq.criterions.FairseqCriterion): the criterion
            optimizer (~fairseq.optim.FairseqOptimizer): the optimizer
            ignore_grad (bool): multiply loss by 0 if this is set to True

        Returns:
            tuple:
                - the loss
                - the sample size, which is used as the denominator for the
                  gradient
                - logging outputs to display while training
        """
        p = self.get_mask_rate()
        sample = self.process_sample(sample, p=p)

        if self.discriminator_optimizer is None:
            params = list(filter(lambda p: p.requires_grad,
                                 criterion.discriminator.parameters()))
            self.discriminator_optimizer = optim.build_optimizer(self.args, params)

        if self._step_counter % self.update_discr_every == 0:
            discriminator_logging_output = self.train_discriminator(model, criterion.discriminator, ignore_grad)
        else:
            discriminator_logging_output = {"loss": -1.}

        loss, sample_size, generator_logging_output = self.generator_train_step(sample, model, criterion, optimizer)

        logging_output = self.merge_logging_outputs(generator_logging_output, discriminator_logging_output)
        self._step_counter += 1
        return loss, sample_size, logging_output

    @staticmethod
    def merge_logging_outputs(generator_logging_outputs, discriminator_logging_outputs):
        generator_logging_outputs['discriminator_loss'] = discriminator_logging_outputs['loss']

        return generator_logging_outputs

    def generator_train_step(self, sample, model, criterion, optimizer, ignore_grad=False):
        """
        Do forward and backward, and return the loss as computed by *criterion*
        for the given *model* and *sample*.

        Args:
            sample (dict): the mini-batch. The format is defined by the
                :class:`~fairseq.data.FairseqDataset`.
            model (~fairseq.models.BaseFairseqModel): the model
            criterion (~fairseq.criterions.FairseqCriterion): the criterion
            optimizer (~fairseq.optim.FairseqOptimizer): the optimizer
            ignore_grad (bool): multiply loss by 0 if this is set to True

        Returns:
            tuple:
                - the loss
                - the sample size, which is used as the denominator for the
                  gradient
                - logging outputs to display while training
        """

        model.train()
        loss, sample_size, logging_output = criterion(model, sample,
                                                      ignore_mask=self.ignore_mask)
        if ignore_grad:
            loss *= 0
        optimizer.backward(loss)

        return loss, sample_size, logging_output

    def get_discriminator_batch_iterator(self, discriminator):
        max_positions = utils.resolve_max_positions(
            self.max_positions(),
            discriminator.max_positions(),
        )
        epoch_itr = self.get_batch_iterator(
            dataset=self.dataset(self.args.train_subset),
            max_tokens=self.args.max_tokens,
            max_sentences=self.args.max_sentences,
            max_positions=max_positions,
            ignore_invalid_inputs=True,
            required_batch_size_multiple=self.args.required_batch_size_multiple,
            seed=self.args.seed,
            num_shards=self.args.distributed_world_size,
            shard_id=self.args.distributed_rank,
            num_workers=self.args.num_workers,
        )
        itr = epoch_itr.next_epoch_itr(
            fix_batches_to_gpus=self.args.fix_batches_to_gpus,
            shuffle=True
        )
        for i, sample in enumerate(itr):
            if i >= self.discriminator_steps:
                return
            sample = utils.move_to_cuda(sample)
            yield sample

    def train_discriminator(self, generator, discriminator, ignore_grad=False):
        logging_output = {}
        for sample in self.get_discriminator_batch_iterator(discriminator):
            p = self.get_mask_rate()
            sample = self.process_sample(sample, p=p)

            generated = self.sequence_generator.generate((generator,),
                                                         sample, substitute=True, mask_token=self.target_dictionary.index('<MASK>'))

            max_len = sample['target'].shape[1]
            tokens = [x[0]['tokens'] for x in generated]
            lengths = [min(max_len, x.shape[0]) for x in tokens]
            generated_tokens = torch.stack(tuple([torch.cat(
                (
                    sample['target'].new_full(
                        (max_len - length,),
                        self.target_dictionary.pad()
                    ),
                    x[:length],
                )
            ) for x, length in zip(tokens, lengths)]))

            sample['generated_tokens'] = generated_tokens
            _, _, logging_output = self.discriminator_train_step(discriminator, sample, ignore_grad=ignore_grad)
            self.discriminator_optimizer.step()
            self.discriminator_optimizer.zero_grad()

        return logging_output

    def discriminator_train_step(self, discriminator, sample, ignore_grad=False):
        discriminator.train()
        loss, sample_size, logging_output = self.discriminator_loss(discriminator, sample)
        if ignore_grad:
            loss *= 0
        self.discriminator_optimizer.backward(loss)
        return loss, sample_size, logging_output

    def valid_step(self, sample, model, criterion):
        p = self.get_mask_rate()
        sample = self.process_sample(sample, p=p)

        model.eval()
        with torch.no_grad():
            loss, sample_size, logging_output = criterion(model, sample)
        return loss, sample_size, logging_output

    def inference_step(self, generator, models, sample, prefix_tokens=None):
        p = self.get_mask_rate()
        sample = self.process_sample(sample, p=p) # pass 1 for real inference

        with torch.no_grad():
            return generator.generate(models, sample,
                                      prefix_tokens=prefix_tokens)
