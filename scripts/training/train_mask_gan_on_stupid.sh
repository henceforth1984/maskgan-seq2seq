#!/usr/bin/env bash
mkdir -p ../../checkpoints/mask_gan_vocalization_on_stupid
cp ../../checkpoints/mle_transformer_vocalization_stupid/checkpoint_last.pt ../../checkpoints/mask_gan_vocalization_on_stupid
CUDA_VISIBLE_DEVICES=0 fairseq-train ../../data/processed/vocalization \
  --user-dir ../../models --task mask_gan --raw-text \
  -a mask_transformer_vocalization --optimizer adam --lr 0.0003 -s he -t voc \
  --dropout 0.3 --max-tokens 4000 \
  --min-lr '1e-06' --lr-scheduler inverse_sqrt --weight-decay 0.0001 \
  --reset-optimizer \
  --warmup-updates 2500 \
  --discriminator-steps 3 \
  --ignore-mask True \
  --update-discr-every 5 \
  --criterion generator_loss --max-epoch 200 \
  --adam-betas '(0.9, 0.98)' --save-dir ../../checkpoints/mask_gan_vocalization_on_stupid \
  --discriminator-path ../../checkpoints/discriminator_vocalization_on_stupid/checkpoint_best.pt --keep-last-epochs 100