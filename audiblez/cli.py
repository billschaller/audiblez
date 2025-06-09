# -*- coding: utf-8 -*-
import argparse
import logging
import sys

from audiblez.voices import available_voices_str, voices


def cli_main():
    voices_str = ", ".join(voices)
    epilog = (
        "example:\n"
        + "  audiblez book.epub -l en-us -v af_sky\n\n"
        + "to run GUI just run:\n"
        "  audiblez-ui\n\n" + "available voices:\n" + available_voices_str
    )
    default_voice = "af_sky"
    parser = argparse.ArgumentParser(
        epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("epub_file_path", help="Path to the epub file")
    parser.add_argument(
        "-v",
        "--voice",
        default=default_voice,
        help=f"Choose narrating voice: {voices_str}",
    )
    parser.add_argument(
        "-p",
        "--pick",
        default=False,
        help=f"Interactively select which chapters to read in the audiobook",
        action="store_true",
    )
    parser.add_argument(
        "-s", "--speed", default=1.0, help=f"Set speed from 0.5 to 2.0", type=float
    )
    parser.add_argument(
        "-g",
        "--gpu",
        default=False,
        help=f"Use GPU in Torch if available",
        action="store_true",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=".",
        help="Output folder for the audiobook and temporary files",
        metavar="FOLDER",
    )
    parser.add_argument(
        "-l",
        "--level",
        default="INFO",
        help="Set logging level",
    )

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    args = parser.parse_args()


    from core import main

    logging.basicConfig(level=args.level)

    main(
        args.epub_file_path,
        args.voice,
        args.pick,
        args.speed,
        args.output,
        gpu=args.gpu,
    )


if __name__ == "__main__":
    cli_main()
