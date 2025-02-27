from scipy.io import wavfile
import numpy as np
import os
import ffmpeg
import uuid
from pathlib import Path
import subprocess

import soundfile as sf
import noisereduce
from pedalboard import (
    Pedalboard,
    NoiseGate,
    Compressor,
    LowShelfFilter,
    Gain,
)
import tempfile


def save_audio_to_wav(rate, y, this_dir, max_duration=None):
    # Determine the bit rate of the source audio.
    bit_depth = y.dtype.itemsize * 8

    # Convert to 16-bit data if necessary.
    if not (bit_depth == 16):
        if bit_depth == 32:
            audio_data = np.asarray(y / np.max(np.abs(y)) * 32767, dtype=np.int16)
        elif bit_depth == 24:
            audio_data = np.asarray((y / (2**8)) // (2**(bit_depth - 16)), dtype=np.int16)
        else: # For other types of bitness we apply the general normalization method.
            max_val = float(np.iinfo(np.int16).max)
            min_val = float(np.iinfo(np.int16).min)
            audio_data = np.asarray(((y - y.min()) / (y.max() - y.min())) * (max_val - min_val) + min_val, dtype=np.int16)
    else:
        # If the data is already in int16 format, use it directly.
        audio_data = np.asarray(y, dtype=np.int16)

    temp_folder = Path(this_dir) / 'temp'

    # print(rate,y)

    os.makedirs(temp_folder, exist_ok=True)

    wav_name = f'speaker_ref_{uuid.uuid4()}.wav'

    original_wav_path = str(temp_folder / wav_name)

     # Save the audio data to a file without changing the sampling rate.
    wavfile.write(original_wav_path, rate, audio_data)

    if max_duration is not None and max_duration != 0:
         output_wav_path = str(temp_folder / f'cut_{wav_name}')
         (
             ffmpeg.input(original_wav_path)
             .output(output_wav_path, t=max_duration)
             .run(overwrite_output=True, quiet=True)
         )
         os.remove(original_wav_path)
         return output_wav_path

    return original_wav_path

def resample_audio(input_wav_path, this_dir, target_rate=22050):
    temp_folder = Path(this_dir) / 'temp'
    temp_folder.mkdir(parents=True, exist_ok=True)

    output_wav_name = f"resampled_audio_{uuid.uuid4()}.wav"
    output_wav_path = temp_folder / output_wav_name
    (
        ffmpeg
        .input(str(input_wav_path))
        .output(str(output_wav_path), ar=target_rate, acodec='pcm_s16le', ac=1)
        .run(overwrite_output=True,quiet=True)
     )

    return str(output_wav_path)

def improve_ref_audio(input_wav_path, this_dir):
    input_wav_path = Path(input_wav_path)
    this_dir = Path(this_dir)

    temp_folder = Path(this_dir) / 'temp'
    temp_folder.mkdir(parents=True, exist_ok=True)

    # Generating output file name
    out_filename = temp_folder / f"{input_wav_path.stem}_refined.wav"

    print(input_wav_path)

    # Applying filters to an audio stream using ffmpeg-python
    (
        ffmpeg
        .input(str(input_wav_path))
        .filter('lowpass', frequency=8000)
        .filter('highpass', frequency=75)
        .filter_('areverse')
        .filter_('silenceremove', start_periods=1, start_silence=0, start_threshold=0.02)
        .filter_('areverse')
        .filter_('silenceremove', start_periods=1, start_silence=0, start_threshold=0.02)
        .output(str(out_filename))
        .overwrite_output()
        .run(quiet=True)  
    )

    return str(out_filename)

def move_and_rename_file(file_path, target_folder_path, new_file_name):
    # Make sure that the new file name contains the correct .wav extension
    if not new_file_name.lower().endswith('.wav'):
        new_file_name += '.wav'

    # Create Path objects for easy handling of paths
    file_path = Path(file_path)
    target_folder_path = Path(target_folder_path)

    # Creating a target directory if it does not exist
    target_folder_path.mkdir(parents=True, exist_ok=True)

    # Full path to the new file in the destination folder
    target_file_path = target_folder_path / new_file_name

    # Move and rename a file
    file_path.rename(target_file_path)


def improve_and_convert_audio(audio_path, type_audio):
    # Read audio file and apply effects via Pedalboard
    audio_data, sample_rate = sf.read(audio_path)

    board = Pedalboard([
        NoiseGate(threshold_db=-30, ratio=1.5, release_ms=250),
        Compressor(threshold_db=12, ratio=2.5),
        LowShelfFilter(cutoff_frequency_hz=400, gain_db=5),
        Gain(gain_db=0),
        
    ])

    reduced_noise = noisereduce.reduce_noise(y=audio_data,
                                                 sr=sample_rate,
                                                 stationary=True,
                                                 prop_decrease=0.75)


    processed_audio = board(reduced_noise.astype('float32'), sample_rate)

    # processed_audio = board(audio_data.astype('float32'), sample_rate)

    # Create a temporary file for the processed audio
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
        sf.write(temp_file.name, processed_audio.T if processed_audio.ndim > 1 else processed_audio , sample_rate)
        temp_file_path = temp_file.name

    # Defining an output file name with a new extension in the same folder
    output_path = f"{os.path.splitext(audio_path)[0]}_improved.{type_audio}"

    # Convert the processed wav file to the target format using FFmpeg
    stream = (
        ffmpeg
        .input(temp_file_path)
        .output(output_path)
        .overwrite_output()
        .run_async(pipe_stdout=True, pipe_stderr=True)
    )

    out,err = stream.communicate()

    if stream.returncode != 0:
             raise Exception(f"FFmpeg error:\n{err.decode()}")

    # Deleting a temporary wav file after it has been used
    os.unlink(temp_file_path)

    return output_path

# RESEMBLE ENHANCE
from scripts.resemble_enhance.enhancer.inference import denoise, enhance
import torch
import torchaudio
import gc

def clear_gpu_cash():
    # del model
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

def resemble_enchance_audio(audio_path,
        use_enhance,
        solver='Midpoint',
        nfe=64,
        tau=0.5,
        chunk_seconds=10,
        chunks_overlap=1,
        denoising=False,
        output_type = "wav"):
    if audio_path is None:
        return None, None

    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    dwav, orig_sr = torchaudio.load(audio_path)
    dwav = dwav.mean(dim=0)

    wav1 = wav2 = dwav.to(device)
    
# Only denoise, temp off
    # if False:
    #    wav1, new_sr = denoise(dwav.cpu(), orig_sr, device)
    #    wav1 = wav1.cpu().numpy()

    if use_enhance:
       lambd = 0.9 if denoising else 0.1
       solver = solver.lower()
       nfe = int(nfe)

       wav2, new_sr = enhance(dwav=dwav.cpu(), sr=orig_sr, device=device,
                              nfe=nfe, chunk_seconds=chunk_seconds,
                              chunks_overlap=chunks_overlap,solver=solver,lambd=lambd,tau=tau)

       wav2 = wav2.cpu().numpy()

    # result_wav1_tuple = None
    result_wav2_tuple=None

# Only denoise, temp off
    # if False:
    #    result_wav1_tuple=(new_sr,wav1)

    if use_enhance:
       result_waw_2_tuple=(new_sr,wav2)

    # Saving the processed file
    # output_file_name = os.path.splitext(audio_path)[0] + '_improved.wav'
    # output_file_path = save_audio_to_wav(new_sr, wav2, Path(audio_path).parent, max_duration=None)
    # output_file_path = save_audio_to_wav(new_sr, wav1, Path(audio_path).parent, max_duration=None)

    rate = new_sr
    y = wav2

    audio_data = np.asarray(y, dtype=np.float32)
    out_folder = Path(audio_path).parent

    # os.makedirs(out_folder, exist_ok=True)

    wav_name = f"{os.path.basename(audio_path).split('.')[0]}_enhance.{output_type}"

    original_wav_path = str(out_folder / wav_name)  

     # Save the audio data to a file without changing the sampling rate.
    wavfile.write(original_wav_path, rate, audio_data)

    print(original_wav_path)

    clear_gpu_cash()

    return original_wav_path
