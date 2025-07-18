#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# audiblez - A program to convert e-books into audiobooks using
# Kokoro-82M model for high-quality text-to-speech synthesis.
# by Claudio Santini 2025 - https://claudio.uk
import platform
import re
import shutil
import subprocess
import time
from io import StringIO
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

import ebooklib
import numpy as np
import soundfile
import spacy
import torch
from bs4 import BeautifulSoup

from device import init_and_get_gpu_device
from ebooklib import epub
from kokoro import KPipeline

from pick import pick
from tabulate import tabulate

from unidecode import unidecode

sample_rate = 24000


def load_spacy():
    if not spacy.util.is_package("xx_ent_wiki_sm"):
        print("Downloading Spacy model xx_ent_wiki_sm...")
        spacy.cli.download("xx_ent_wiki_sm")


def main(
    file_path,
    voice,
    pick_manually,
    speed,
    output_folder=".",
    max_chapters=None,
    max_sentences=None,
    selected_chapters=None,
    post_event=None,
    gpu=False,
):
    if post_event:
        post_event("CORE_STARTED")
    load_spacy()
    if output_folder != '.':
        Path(output_folder).mkdir(parents=True, exist_ok=True)
    output_folder = Path(output_folder).absolute()

    filename = Path(file_path).name

    extension = '.epub'
    book = epub.read_epub(file_path)
    meta_title = book.get_metadata('DC', 'title')
    title = meta_title[0][0] if meta_title else ''
    meta_creator = book.get_metadata('DC', 'creator')
    creator = meta_creator[0][0] if meta_creator else ''

    cover_maybe = find_cover(book)
    cover_image = cover_maybe.get_content() if cover_maybe else b""
    if cover_maybe:
        print(f'Found cover image {cover_maybe.file_name} in {cover_maybe.media_type} format')

    document_chapters = find_document_chapters_and_extract_texts(book)

    if not selected_chapters:
        if pick_manually is True:
            selected_chapters = pick_chapters(document_chapters)
        else:
            selected_chapters = find_good_chapters(document_chapters)
    print_selected_chapters(document_chapters, selected_chapters)
    texts = [c.extracted_text for c in selected_chapters]

    has_ffmpeg = shutil.which('ffmpeg') is not None
    if not has_ffmpeg:
        print('\033[91m' + 'ffmpeg not found. Please install ffmpeg to create mp3 and m4b audiobook files.' + '\033[0m')

    stats = SimpleNamespace(
        total_chars=sum(map(len, texts)),
        processed_chars=0,
        chars_per_sec=500 if (torch.cuda.is_available() or torch.xpu.is_available()) else 50)
    print('Started at:', time.strftime('%H:%M:%S'))
    print(f'Total characters: {stats.total_chars:,}')
    print('Total words:', len(' '.join(texts).split()))
    eta = strfdelta((stats.total_chars - stats.processed_chars) / stats.chars_per_sec)
    print(f"Estimated time remaining (assuming {stats.chars_per_sec} chars/sec): {eta}")
    device = "cpu"
    if gpu:
        device = init_and_get_gpu_device()
    pipeline = KPipeline(
        lang_code=voice[0], device=device
    )  # a for american or b for british etc.

    chapter_wav_files = []
    for i, chapter in enumerate(selected_chapters, start=1):
        if max_chapters and i > max_chapters: break
        text = chapter.extracted_text
        xhtml_file_name = chapter.get_name().replace(' ', '_').replace('/', '_').replace('\\', '_')
        chapter_wav_path = Path(output_folder) / filename.replace(extension, f'_chapter_{i}_{voice}_{xhtml_file_name}.wav')
        chapter_wav_files.append(chapter_wav_path)
        if Path(chapter_wav_path).exists():
            print(f'File for chapter {i} already exists. Skipping')
            stats.processed_chars += len(text)
            if post_event:
                post_event('CORE_CHAPTER_FINISHED', chapter_index=chapter.chapter_index)
            continue
        if len(text.strip()) < 10:
            print(f'Skipping empty chapter {i}')
            chapter_wav_files.remove(chapter_wav_path)
            continue
        if i == 1:
            # add intro text
            text = f'{title} – {creator}.\n\n' + text
        start_time = time.time()
        if post_event: post_event('CORE_CHAPTER_STARTED', chapter_index=chapter.chapter_index)
        audio_segments = gen_audio_segments(
            pipeline, text, voice, speed, stats, post_event=post_event, max_sentences=max_sentences)
        if audio_segments:
            final_audio = np.concatenate(audio_segments)
            soundfile.write(chapter_wav_path, final_audio, sample_rate)
            end_time = time.time()
            delta_seconds = end_time - start_time
            chars_per_sec = len(text) / delta_seconds
            print('Chapter written to', chapter_wav_path)
            if post_event: post_event('CORE_CHAPTER_FINISHED', chapter_index=chapter.chapter_index)
            print(f'Chapter {i} read in {delta_seconds:.2f} seconds ({chars_per_sec:.0f} characters per second)')
        else:
            print(f'Warning: No audio generated for chapter {i}')
            chapter_wav_files.remove(chapter_wav_path)

    if has_ffmpeg:
        create_index_file(title, creator, chapter_wav_files, output_folder)
        create_m4b(chapter_wav_files, filename, cover_image, output_folder)
        if post_event: post_event('CORE_FINISHED')


def find_cover(book):
    def is_image(item):
        return item is not None and item.media_type.startswith('image/')

    for item in book.get_items_of_type(ebooklib.ITEM_COVER):
        if is_image(item):
            return item

    # https://idpf.org/forum/topic-715
    for meta in book.get_metadata('OPF', 'cover'):
        if is_image(item := book.get_item_with_id(meta[1]['content'])):
            return item

    if is_image(item := book.get_item_with_id('cover')):
        return item

    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        if 'cover' in item.get_name().lower() and is_image(item):
            return item

    return None


def print_selected_chapters(document_chapters, chapters):
    ok = 'X' if platform.system() == 'Windows' else '✅'
    print(tabulate([
        [i, c.get_name(), len(c.extracted_text), ok if c in chapters else '', chapter_beginning_one_liner(c)]
        for i, c in enumerate(document_chapters, start=1)
    ], headers=['#', 'Chapter', 'Text Length', 'Selected', 'First words']))

def is_audible_text(text):
    clean_text = re.sub(r'[^a-zA-Z0-9]', '', text)
    return len(clean_text) > 1

def merge_texts(sentences, max_length):
    out_sentences = []
    text = ''
    for sentence in sentences:
        sentence = unidecode(sentence)
        if not is_audible_text(sentence):
            continue
        
        if len(sentence) > max_length:
            print(f"WARNING: sentence is greater than max length! '{sentence}'")
            if len(text) > 0:
                out_sentences.append(text)
                text = ''
            out_sentences.append(sentence)
        elif len(sentence) + len(text) > max_length:
            out_sentences.append(text)
            text = sentence.strip()
        else:
            text += ' ' + sentence.strip()

    return out_sentences


def gen_audio_segments(pipeline, text, voice, speed, stats=None, max_sentences=None, post_event=None):
    nlp = spacy.load('xx_ent_wiki_sm')
    nlp.add_pipe('sentencizer')
    audio_segments = []
    doc = nlp(text)
    #sentences = merge_texts(list(doc.sents)[:max_sentences], 500)
    sentences = list(doc.sents)[:max_sentences]
    print(sentences)
    texts = merge_texts([s.text for s in sentences], 500)
    for gs, ps, audio in pipeline(texts, voice=voice, speed=speed, split_pattern=r'\n\n\n'):
        audio_segments.append(audio)
        try:
            print(f"Graphemes: {gs}, Phonemes: {ps}")
        except:
            print("Failed to print graphemes and phonemes...")
        # if stats:
        #     stats.processed_chars += len(sent.text)
        #     stats.progress = stats.processed_chars * 100 // stats.total_chars
        #     stats.eta = strfdelta((stats.total_chars - stats.processed_chars) / stats.chars_per_sec)
        #     if post_event: post_event('CORE_PROGRESS', stats=stats)
        #     print(f'Estimated time remaining: {stats.eta}')
        #     print('Progress:', f'{stats.progress}%\n')
    return audio_segments


def gen_text(text, voice='af_heart', output_file='text.wav', speed=1, play=False, gpu=False):
    lang_code = voice[:1]
    device = "cpu"
    if gpu:
        device = init_and_get_gpu_device()
    pipeline = KPipeline(lang_code=lang_code, device=device)
    load_spacy()
    audio_segments = gen_audio_segments(pipeline, text, voice=voice, speed=speed);
    final_audio = np.concatenate(audio_segments)
    soundfile.write(output_file, final_audio, sample_rate)
    if play:
        subprocess.run(['ffplay', '-autoexit', '-nodisp', output_file])


def find_document_chapters_and_extract_texts(book):
    """Returns every chapter that is an ITEM_DOCUMENT and enriches each chapter with extracted_text."""
    document_chapters = []
    for chapter in book.get_items():
        if chapter.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        xml = chapter.get_body_content()
        soup = BeautifulSoup(xml, features='lxml')
        chapter.extracted_text = ''
        html_content_tags = ['title', 'p', 'h1', 'h2', 'h3', 'h4', 'li']
        for text in [c.text.strip() for c in soup.find_all(html_content_tags) if c.text]:
            if not text.endswith('.'):
                text += '.'
            chapter.extracted_text += text + '\n'
        document_chapters.append(chapter)
    for i, c in enumerate(document_chapters):
        c.chapter_index = i  # this is used in the UI to identify chapters
    return document_chapters


def is_chapter(c):
    name = c.get_name().lower()
    has_min_len = len(c.extracted_text) > 100
    title_looks_like_chapter = bool(
        'chapter' in name.lower()
        or re.search(r'part_?\d{1,3}', name)
        or re.search(r'split_?\d{1,3}', name)
        or re.search(r'ch_?\d{1,3}', name)
        or re.search(r'chap_?\d{1,3}', name)
    )
    return has_min_len and title_looks_like_chapter


def chapter_beginning_one_liner(c, chars=20):
    s = c.extracted_text[:chars].strip().replace('\n', ' ').replace('\r', ' ')
    return s + '…' if len(s) > 0 else ''


def find_good_chapters(document_chapters):
    chapters = [c for c in document_chapters if c.get_type() == ebooklib.ITEM_DOCUMENT and is_chapter(c)]
    if len(chapters) == 0:
        print('Not easy to recognize the chapters, defaulting to all non-empty documents.')
        chapters = [c for c in document_chapters if c.get_type() == ebooklib.ITEM_DOCUMENT and len(c.extracted_text) > 10]
    return chapters


def pick_chapters(chapters):
    # Display the document name, the length and first 50 characters of the text
    chapters_by_names = {
        f'{c.get_name()}\t({len(c.extracted_text)} chars)\t[{chapter_beginning_one_liner(c, 50)}]': c
        for c in chapters}
    title = 'Select which chapters to read in the audiobook'
    ret = pick(list(chapters_by_names.keys()), title, multiselect=True, min_selection_count=1)
    selected_chapters_out_of_order = [chapters_by_names[r[0]] for r in ret]
    selected_chapters = [c for c in chapters if c in selected_chapters_out_of_order]
    return selected_chapters


def strfdelta(tdelta, fmt='{D:02}d {H:02}h {M:02}m {S:02}s'):
    remainder = int(tdelta)
    f = Formatter()
    desired_fields = [field_tuple[1] for field_tuple in f.parse(fmt)]
    possible_fields = ('W', 'D', 'H', 'M', 'S')
    constants = {'W': 604800, 'D': 86400, 'H': 3600, 'M': 60, 'S': 1}
    values = {}
    for field in possible_fields:
        if field in desired_fields and field in constants:
            values[field], remainder = divmod(remainder, constants[field])
    return f.format(fmt, **values)


def concat_wavs_with_ffmpeg(chapter_files, output_folder, filename):
    print(output_folder)
    print(filename)
    wav_list_txt = Path(output_folder) / filename.replace('.epub', '_wav_list.txt')
    with open(wav_list_txt, 'w') as f:
        for wav_file in chapter_files:
            print(f"file '{wav_file}'")
            f.write(f"file '{wav_file}'\n")

    concat_file_path = Path(output_folder) / filename.replace('.epub', '.tmp.mp4')
    subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', wav_list_txt, '-c:a', 'libopus', '-b:a', '64k', concat_file_path])
    #Path(wav_list_txt).unlink()
    return concat_file_path


def create_m4b(chapter_files, filename, cover_image, output_folder):
    concat_file_path = concat_wavs_with_ffmpeg(chapter_files, output_folder, filename)
    final_filename = Path(output_folder) / filename.replace('.epub', '.m4b')
    chapters_txt_path = Path(output_folder) / "chapters.txt"
    print('Creating M4B file...')

    if cover_image:
        cover_file_path = Path(output_folder) / 'cover'
        with open(cover_file_path, 'wb') as f:
            f.write(cover_image)
        cover_image_args = ["-i", f'{cover_file_path}', "-map", "0:a", "-map", "2:v"]
    else:
        cover_image_args = []

    proc = subprocess.run([
        'ffmpeg',
        '-y',  # overwrite output file without asking
        '-i', f'{concat_file_path}',  # input 0 file (audio)
        '-i', f'{chapters_txt_path}',  # input 1 file (chapters)
        *cover_image_args,  # cover image
        '-map', '0',  # map all streams from input 0
        '-map_metadata', '1',  # map metadata from input 1
        '-c:a', 'copy',  # copy audio codec
        '-c:v', 'copy',  # copy video codec
        '-disposition:v', 'attached_pic',  # attach cover image
        '-c', 'copy',  # copy codec
        '-f', 'mp4',  # format
        f'{final_filename}'  # output file
    ])
    Path(concat_file_path).unlink()
    if proc.returncode == 0:
        print(f'{final_filename} created. Enjoy your audiobook.')
        print('Feel free to delete the intermediary .wav chapter files, the .m4b is all you need.')


def probe_duration(file_name):
    args = ['ffprobe', '-i', file_name, '-show_entries', 'format=duration', '-v', 'quiet', '-of', 'default=noprint_wrappers=1:nokey=1']
    proc = subprocess.run(args, capture_output=True, text=True, check=True)
    return float(proc.stdout.strip())


def create_index_file(title, creator, chapter_mp3_files, output_folder):
    with open(Path(output_folder) / "chapters.txt", "w", encoding="utf-8") as f:
        f.write(f";FFMETADATA1\ntitle={title}\nartist={creator}\n\n")
        start = 0
        i = 0
        for c in chapter_mp3_files:
            duration = probe_duration(c)
            end = start + (int)(duration * 1000)
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={end}\ntitle=Chapter {i}\n\n")
            i += 1
            start = end


def unmark_element(element, stream=None):
    """auxiliarry function to unmark markdown text"""
    if stream is None:
        stream = StringIO()
    if element.text:
        stream.write(element.text)
    for sub in element:
        unmark_element(sub, stream)
    if element.tail:
        stream.write(element.tail)
    return stream.getvalue()


def unmark(text):
    """Unmark markdown text"""
    Markdown.output_formats["plain"] = unmark_element  # patching Markdown
    __md = Markdown(output_format="plain")
    __md.stripTopLevelTags = False
    return __md.convert(text)
