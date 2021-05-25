#!/usr/bin/evn python3
"""Build Speech Recognition pipeline based on fairseq's wav2vec2.0 and dump it to TorchScript file.

To use this script, you need `fairseq`.
"""
import os
import argparse
import logging

import torch
from torch.utils.mobile_optimizer import optimize_for_mobile
import torchaudio
from torchaudio.models.wav2vec2.utils.import_fairseq import import_fairseq_finetuned_model
import fairseq
import simple_ctc

_LG = logging.getLogger(__name__)


def _parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
    )
    parser.add_argument(
        '--model-file',
        required=True,
        help='Path to the input pretrained weight file.'
    )
    parser.add_argument(
        '--dict-dir',
        help=(
            'Path to the directory in which `dict.ltr.txt` file is found. '
            'Required only when the model is finetuned.'
        )
    )
    parser.add_argument(
        '--output-path',
        help='Path to the directory, where the TorchScript-ed pipelines are saved.',
    )
    parser.add_argument(
        '--test-file',
        help='Path to a test audio file.',
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help=(
            'When enabled, individual components are separately tested '
            'for the numerical compatibility and TorchScript compatibility.'
        )
    )
    parser.add_argument(
        '--quantize',
        action='store_true',
        help='Apply quantization to model.'
    )
    parser.add_argument(
        '--optimize-for-mobile',
        action='store_true',
        help='Apply optmization for mobile.'
    )
    return parser.parse_args()


class Loader(torch.nn.Module):
    def forward(self, audio_path: str) -> torch.Tensor:
        waveform, sample_rate = torchaudio.load(audio_path)
        if sample_rate != 16000:
            waveform = torchaudio.functional.resample(waveform, float(sample_rate), 16000.)
        return waveform


class Encoder(torch.nn.Module):
    def __init__(self, encoder: torch.nn.Module):
        super().__init__()
        self.encoder = encoder

    def forward(self, waveform: torch.Tensor) -> torch.Tensor:
        result, _ = self.encoder(waveform)
        return result


class Decoder(torch.nn.Module):
    def __init__(self, decoder: torch.nn.Module):
        super().__init__()
        self.decoder = decoder

    def forward(self, emission: torch.Tensor) -> str:
        result = self.decoder.decode(emission)
        return ''.join(result.label_sequences[0][0]).replace('|', ' ')


def _get_decoder():
    labels = [
        "<s>",
        "<pad>",
        "</s>",
        "<unk>",
        "|",
        "E",
        "T",
        "A",
        "O",
        "N",
        "I",
        "H",
        "S",
        "R",
        "D",
        "L",
        "U",
        "M",
        "W",
        "C",
        "F",
        "G",
        "Y",
        "P",
        "B",
        "V",
        "K",
        "'",
        "X",
        "J",
        "Q",
        "Z",
    ]

    return Decoder(
        simple_ctc.BeamSearchDecoder(
            labels,
            cutoff_top_n=40,
            cutoff_prob=0.8,
            beam_size=100,
            num_processes=1,
            blank_id=0,
            is_nll=True,
        )
    )


def _load_fairseq_model(input_file, data_dir=None):
    overrides = {}
    if data_dir:
        overrides['data'] = data_dir

    model, args, _ = fairseq.checkpoint_utils.load_model_ensemble_and_task(
        [input_file], arg_overrides=overrides
    )
    model = model[0]
    return model, args


def _get_model(model_file, dict_dir):
    original, args = _load_fairseq_model(model_file, dict_dir)
    model = import_fairseq_finetuned_model(original, args)
    return model


def _main():
    args = _parse_args()
    _init_logging(args.debug)
    loader = Loader()
    model = _get_model(args.model_file, args.dict_dir).eval()
    encoder = Encoder(model)
    decoder = _get_decoder()
    _LG.info(encoder)

    if args.quantize:
        _LG.info('Quantizing the model')
        model.encoder.transformer.pos_conv_embed.__prepare_scriptable__()
        encoder = torch.quantization.quantize_dynamic(
            encoder, qconfig_spec={torch.nn.Linear}, dtype=torch.qint8)
        _LG.info(encoder)

    # test
    if args.test_file:
        _LG.info('Testing with %s', args.test_file)
        waveform = loader(args.test_file)
        emission = encoder(waveform)
        transcript = decoder(emission)
        _LG.info(transcript)

    torch.jit.script(loader).save(os.path.join(args.output_path, 'loader.zip'))
    torch.jit.script(decoder).save(os.path.join(args.output_path, 'decoder.zip'))
    scripted = torch.jit.script(encoder)
    if args.optimize_for_mobile:
        scripted = optimize_for_mobile(scripted)
    scripted.save(os.path.join(args.output_path, 'encoder.zip'))


def _init_logging(debug=False):
    level = logging.DEBUG if debug else logging.INFO
    format_ = (
        '%(message)s' if not debug else
        '%(asctime)s: %(levelname)7s: %(funcName)10s: %(message)s'
    )
    logging.basicConfig(level=level, format=format_)


if __name__ == '__main__':
    _main()
