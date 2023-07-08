#!/usr/bin/python3

"""
This tool is for checking the security hardening options of the Linux kernel.

Author: Alexander Popov <alex.popov@linux.com>

This module performs input/output.
"""

# pylint: disable=missing-function-docstring,line-too-long,invalid-name,too-many-branches,too-many-statements

import gzip
import sys
from argparse import ArgumentParser
from collections import OrderedDict
import re
import json
from .__about__ import __version__
from .checks import add_kconfig_checks, add_cmdline_checks, normalize_cmdline_options, add_sysctl_checks
from .engine import populate_with_data, perform_checks, override_expected_value


def _open(file: str, *args, **kwargs):
    open_method = open
    if file.endswith(".gz"):
        open_method = gzip.open

    return open_method(file, *args, **kwargs)


def detect_arch(fname, archs):
    with _open(fname, 'rt', encoding='utf-8') as f:
        arch_pattern = re.compile("CONFIG_[a-zA-Z0-9_]*=y")
        arch = None
        for line in f.readlines():
            if arch_pattern.match(line):
                option, _ = line[7:].split('=', 1)
                if option in archs:
                    if arch is None:
                        arch = option
                    else:
                        return None, 'more than one supported microarchitecture is detected'
        if arch is None:
            return None, 'failed to detect microarchitecture'
        return arch, 'OK'


def detect_kernel_version(fname):
    with _open(fname, 'rt', encoding='utf-8') as f:
        ver_pattern = re.compile("# Linux/.* Kernel Configuration")
        for line in f.readlines():
            if ver_pattern.match(line):
                line = line.strip()
                parts = line.split()
                ver_str = parts[2]
                ver_numbers = ver_str.split('.')
                if len(ver_numbers) < 3 or not ver_numbers[0].isdigit() or not ver_numbers[1].isdigit():
                    msg = f'failed to parse the version "{ver_str}"'
                    return None, msg
                return (int(ver_numbers[0]), int(ver_numbers[1])), None
        return None, 'no kernel version detected'


def detect_compiler(fname):
    gcc_version = None
    clang_version = None
    with _open(fname, 'rt', encoding='utf-8') as f:
        gcc_version_pattern = re.compile("CONFIG_GCC_VERSION=[0-9]*")
        clang_version_pattern = re.compile("CONFIG_CLANG_VERSION=[0-9]*")
        for line in f.readlines():
            if gcc_version_pattern.match(line):
                gcc_version = line[19:-1]
            if clang_version_pattern.match(line):
                clang_version = line[21:-1]
    if gcc_version is None or clang_version is None:
        return None, 'no CONFIG_GCC_VERSION or CONFIG_CLANG_VERSION'
    if gcc_version == '0' and clang_version != '0':
        return 'CLANG ' + clang_version, 'OK'
    if gcc_version != '0' and clang_version == '0':
        return 'GCC ' + gcc_version, 'OK'
    sys.exit(f'[!] ERROR: invalid GCC_VERSION and CLANG_VERSION: {gcc_version} {clang_version}')


def print_unknown_options(checklist, parsed_options):
    known_options = []

    for o1 in checklist:
        if o1.type != 'complex':
            known_options.append(o1.name)
            continue
        for o2 in o1.opts:
            if o2.type != 'complex':
                if hasattr(o2, 'name'):
                    known_options.append(o2.name)
                continue
            for o3 in o2.opts:
                assert(o3.type != 'complex'), \
                       f'unexpected ComplexOptCheck inside {o2.name}'
                if hasattr(o3, 'name'):
                    known_options.append(o3.name)

    for option, value in parsed_options.items():
        if option not in known_options:
            print(f'[?] No check for option {option} ({value})')


def print_checklist(mode, checklist, with_results):
    if mode == 'json':
        output = []
        for opt in checklist:
            output.append(opt.json_dump(with_results))
        print(json.dumps(output))
        return

    # table header
    sep_line_len = 91
    if with_results:
        sep_line_len += 30
    print('=' * sep_line_len)
    print(f'{"option name":^40}|{"type":^7}|{"desired val":^12}|{"decision":^10}|{"reason":^18}', end='')
    if with_results:
        print('| check result', end='')
    print()
    print('=' * sep_line_len)

    # table contents
    for opt in checklist:
        if with_results:
            if mode == 'show_ok':
                if not opt.result.startswith('OK'):
                    continue
            if mode == 'show_fail':
                if not opt.result.startswith('FAIL'):
                    continue
        opt.table_print(mode, with_results)
        print()
        if mode == 'verbose':
            print('-' * sep_line_len)
    print()

    # final score
    if with_results:
        fail_count = len(list(filter(lambda opt: opt.result.startswith('FAIL'), checklist)))
        fail_suppressed = ''
        ok_count = len(list(filter(lambda opt: opt.result.startswith('OK'), checklist)))
        ok_suppressed = ''
        if mode == 'show_ok':
            fail_suppressed = ' (suppressed in output)'
        if mode == 'show_fail':
            ok_suppressed = ' (suppressed in output)'
        print(f'[+] Config check is finished: \'OK\' - {ok_count}{ok_suppressed} / \'FAIL\' - {fail_count}{fail_suppressed}')


def parse_kconfig_file(parsed_options, fname):
    with _open(fname, 'rt', encoding='utf-8') as f:
        opt_is_on = re.compile("CONFIG_[a-zA-Z0-9_]*=[a-zA-Z0-9_\"]*")
        opt_is_off = re.compile("# CONFIG_[a-zA-Z0-9_]* is not set")

        for line in f.readlines():
            line = line.strip()
            option = None
            value = None

            if opt_is_on.match(line):
                option, value = line.split('=', 1)
                if value == 'is not set':
                    sys.exit(f'[!] ERROR: bad enabled Kconfig option "{line}"')
            elif opt_is_off.match(line):
                option, value = line[2:].split(' ', 1)
                if value != 'is not set':
                    sys.exit(f'[!] ERROR: bad disabled Kconfig option "{line}"')

            if option in parsed_options:
                sys.exit(f'[!] ERROR: Kconfig option "{line}" exists multiple times')

            if option:
                parsed_options[option] = value


def parse_cmdline_file(parsed_options, fname):
    with open(fname, 'r', encoding='utf-8') as f:
        line = f.readline()
        opts = line.split()

        line = f.readline()
        if line:
            sys.exit(f'[!] ERROR: more than one line in "{fname}"')

        for opt in opts:
            if '=' in opt:
                name, value = opt.split('=', 1)
            else:
                name = opt
                value = '' # '' is not None
            value = normalize_cmdline_options(name, value)
            parsed_options[name] = value


def parse_sysctl_file(parsed_options, fname):
    print('parse_sysctl_file: TODO')


def main():
    # Report modes:
    #   * verbose mode for
    #     - reporting about unknown kernel options in the Kconfig
    #     - verbose printing of ComplexOptCheck items
    #   * json mode for printing the results in JSON format
    report_modes = ['verbose', 'json', 'show_ok', 'show_fail']
    supported_archs = ['X86_64', 'X86_32', 'ARM64', 'ARM']
    parser = ArgumentParser(prog='kconfig-hardened-check',
                            description='A tool for checking the security hardening options of the Linux kernel')
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    parser.add_argument('-m', '--mode', choices=report_modes,
                        help='choose the report mode')
    parser.add_argument('-c', '--config',
                        help='check the security hardening options in the kernel Kconfig file (also supports *.gz files)')
    parser.add_argument('-l', '--cmdline',
                        help='check the security hardening options in the kernel cmdline file (contents of /proc/cmdline)')
#   parser.add_argument('-s', '--sysctl',
#                       help='check the security hardening options in the sysctl output file (`sudo sysctl -a > file`)')
    parser.add_argument('-p', '--print', choices=supported_archs,
                        help='print the security hardening recommendations for the selected microarchitecture')
    parser.add_argument('-g', '--generate', choices=supported_archs,
                        help='generate a Kconfig fragment with the security hardening options for the selected microarchitecture')
    args = parser.parse_args()
    args.sysctl = None # FIXME

    mode = None
    if args.mode:
        mode = args.mode
        if mode != 'json':
            print(f'[+] Special report mode: {mode}')

    config_checklist = []

    if args.config:
        if args.print:
            sys.exit('[!] ERROR: --config and --print can\'t be used together')

        if args.generate:
            sys.exit('[!] ERROR: --config and --generate can\'t be used together')

        if mode != 'json':
            print(f'[+] Kconfig file to check: {args.config}')
            if args.cmdline:
                print(f'[+] Kernel cmdline file to check: {args.cmdline}')
            if args.sysctl:
                print(f'[+] Kernel sysctl output file to check: {args.sysctl}')

        arch, msg = detect_arch(args.config, supported_archs)
        if arch is None:
            sys.exit(f'[!] ERROR: {msg}')
        if mode != 'json':
            print(f'[+] Detected microarchitecture: {arch}')

        kernel_version, msg = detect_kernel_version(args.config)
        if kernel_version is None:
            sys.exit(f'[!] ERROR: {msg}')
        if mode != 'json':
            print(f'[+] Detected kernel version: {kernel_version[0]}.{kernel_version[1]}')

        compiler, msg = detect_compiler(args.config)
        if mode != 'json':
            if compiler:
                print(f'[+] Detected compiler: {compiler}')
            else:
                print(f'[-] Can\'t detect the compiler: {msg}')

        # add relevant Kconfig checks to the checklist
        add_kconfig_checks(config_checklist, arch)

        if args.cmdline:
            # add relevant cmdline checks to the checklist
            add_cmdline_checks(config_checklist, arch)

        if args.sysctl:
            # add relevant sysctl checks to the checklist
            add_sysctl_checks(config_checklist, arch)

        # populate the checklist with the parsed Kconfig data
        parsed_kconfig_options = OrderedDict()
        parse_kconfig_file(parsed_kconfig_options, args.config)
        populate_with_data(config_checklist, parsed_kconfig_options, 'kconfig')

        # populate the checklist with the kernel version data
        populate_with_data(config_checklist, kernel_version, 'version')

        if args.cmdline:
            # populate the checklist with the parsed cmdline data
            parsed_cmdline_options = OrderedDict()
            parse_cmdline_file(parsed_cmdline_options, args.cmdline)
            populate_with_data(config_checklist, parsed_cmdline_options, 'cmdline')

        if args.sysctl:
            # populate the checklist with the parsed sysctl data
            parsed_sysctl_options = OrderedDict()
            parse_sysctl_file(parsed_sysctl_options, args.sysctl)
            populate_with_data(config_checklist, parsed_sysctl_options, 'sysctl')

        # hackish refinement of the CONFIG_ARCH_MMAP_RND_BITS check
        mmap_rnd_bits_max = parsed_kconfig_options.get('CONFIG_ARCH_MMAP_RND_BITS_MAX', None)
        if mmap_rnd_bits_max:
            override_expected_value(config_checklist, 'CONFIG_ARCH_MMAP_RND_BITS', mmap_rnd_bits_max)

        # now everything is ready, perform the checks
        perform_checks(config_checklist)

        if mode == 'verbose':
            # print the parsed options without the checks (for debugging)
            all_parsed_options = parsed_kconfig_options # assignment does not copy
            if args.cmdline:
                all_parsed_options.update(parsed_cmdline_options)
            if args.sysctl:
                all_parsed_options.update(parsed_sysctl_options)
            print_unknown_options(config_checklist, all_parsed_options)

        # finally print the results
        print_checklist(mode, config_checklist, True)

        sys.exit(0)
    elif args.cmdline:
        sys.exit('[!] ERROR: checking cmdline depends on checking Kconfig')
    elif args.sysctl:
        # TODO: sysctl check should also work separately
        sys.exit('[!] ERROR: checking sysctl depends on checking Kconfig')

    if args.print:
        assert(args.config is None and args.cmdline is None and args.sysctl is None), 'unexpected args'
        if mode and mode not in ('verbose', 'json'):
            sys.exit(f'[!] ERROR: wrong mode "{mode}" for --print')
        arch = args.print
        add_kconfig_checks(config_checklist, arch)
        add_cmdline_checks(config_checklist, arch)
        add_sysctl_checks(config_checklist, arch)
        if mode != 'json':
            print(f'[+] Printing kernel security hardening options for {arch}...')
        print_checklist(mode, config_checklist, False)
        sys.exit(0)

    if args.generate:
        assert(args.config is None and args.cmdline is None and args.sysctl is None), 'unexpected args'
        if mode:
            sys.exit(f'[!] ERROR: wrong mode "{mode}" for --generate')
        arch = args.generate
        add_kconfig_checks(config_checklist, arch)
        print(f'CONFIG_{arch}=y') # the Kconfig fragment should describe the microarchitecture
        for opt in config_checklist:
            if opt.name == 'CONFIG_ARCH_MMAP_RND_BITS':
                continue # don't add CONFIG_ARCH_MMAP_RND_BITS because its value needs refinement
            if opt.expected == 'is not set':
                print(f'# {opt.name} is not set')
            else:
                print(f'{opt.name}={opt.expected}')
        sys.exit(0)

    parser.print_help()
    sys.exit(0)
