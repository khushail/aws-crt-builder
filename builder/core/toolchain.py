# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0.

import os
import re
from builder.core.data import COMPILERS
from builder.core.host import current_os, current_arch, normalize_target, normalize_arch
from builder.core import util

# helpful list of XCode clang output: https://gist.github.com/yamaya/2924292


def _compiler_version(cc):
    if current_os() != 'windows':
        result = util.run_command(cc, '--version', quiet=True)
        lines = result.output.split('\n')

        for text in lines:
            # Apple clang
            m = re.match('Apple (LLVM|clang) version (\d+)', text)
            if m:
                return 'appleclang', m.group(2)
            # LLVM clang
            m = re.match('.*(LLVM|clang) version (\d+)', text)
            if m:
                return 'clang', m.group(2)
            # GCC 4.x
            m = re.match('gcc .+ (4\.\d+)', text)
            if m:
                return 'gcc', m.group(1)
            # GCC 5+
            m = re.match('gcc .+ (\d+)\.', text)
            if m:
                return 'gcc', m.group(1)
    return None, None


def _find_compiler_tool(name, versions):
    # look for the default tool, and see if the version is in the search set
    path = util.where(name, resolve_symlinks=False)
    if path:
        version = _compiler_version(path)[1]
        if version in versions:
            return path, version
    for version in versions:
        for pattern in ('{name}-{version}', '{name}-{version}.0'):
            exe = pattern.format(name=name, version=version)
            path = util.where(exe, resolve_symlinks=False)
            if path:
                return path, version
    return None, None


def _clang_versions():
    versions = [v for v in COMPILERS['clang']
                ['versions'].keys() if v != 'default']
    versions.sort()
    versions.reverse()
    return versions


def _appleclang_versions():
    versions = [v for v in COMPILERS['appleclang']
                ['versions'].keys() if v != 'default']
    versions.sort()
    versions.reverse()
    return versions


def _gcc_versions():
    versions = [v for v in COMPILERS['gcc']
                ['versions'].keys() if v != 'default']
    versions.sort()
    versions.reverse()
    return versions


def _msvc_versions():
    versions = [v for v in COMPILERS['msvc']
                ['versions'].keys() if v != 'default']
    # sorted high to low by int value
    versions.sort(key=lambda x: int(x), reverse=True)
    return versions


def _is_cross_compile(target_os, target_arch):
    # Mac compiling for anything that isn't iOS or itself
    if current_os() == 'macos' and target_os in ["macos", "ios"]:
        return False
    # Windows is never a cross compile, just toolset swap
    if current_os() == 'windows' and target_os == 'windows':
        return False
    if target_os != current_os() or normalize_arch(target_arch) != current_arch():
        return True
    return False


class Toolchain(object):
    """ Represents a compiler toolchain """

    def __init__(self, **kwargs):
        if 'default' in kwargs or len(kwargs) == 0:
            for slot in ('host', 'target', 'arch', 'compiler', 'compiler_version'):
                setattr(self, slot, 'default')

        if 'spec' in kwargs:
            spec = kwargs['spec']
            self.host = spec.host
            self.compiler = spec.compiler
            self.compiler_version = spec.compiler_version
            self.target = spec.target
            self.arch = normalize_arch(spec.arch)

        # Pull out individual fields. Note this is not in an else to support overriding at construction time
        for slot in ('host', 'target', 'arch', 'compiler', 'compiler_version'):
            if slot in kwargs:
                setattr(self, slot, kwargs[slot])

        if self.target == 'default':
            self.target = current_os()
        if self.arch == 'default':
            self.arch = current_arch()

        # detect cross-compile
        self.cross_compile = _is_cross_compile(self.target, self.arch)
        self.platform = normalize_target(
            '{}-{}'.format(self.target, self.arch))
        self.shell_env = []

        if self.cross_compile:
            print('Setting compiler to gcc for cross compile')
            self.compiler = 'gcc'
            # it's really 4.9, but don't need a separate entry for that
            self.compiler_version = '4.8'
        else:
            # resolve default compiler and/or version
            if self.compiler == 'default':
                c, v = Toolchain.default_compiler()
                if c and v:
                    self.compiler, self.compiler_version = c, v
            elif self.compiler_version == 'default':
                self.compiler_version = _compiler_version(
                    self.compiler_path())[1]
                if not self.compiler_version:
                    self.compiler_version = 'default'

        self.name = '-'.join([self.host, self.compiler,
                              self.compiler_version, self.target, self.arch])

    def compiler_path(self):
        assert not self.cross_compile
        if self.compiler == 'default':
            return Toolchain.default_compiler()[0]
        return Toolchain.find_compiler(self.compiler, self.compiler_version if self.compiler_version != 'default' else None)[0]

    def cxx_compiler_path(self):
        assert not self.cross_compile
        compiler = self.compiler
        if self.compiler == 'default':
            compiler = Toolchain.default_compiler()[0]
        if compiler == 'clang' or compiler == 'appleclang':
            return Toolchain.find_compiler_tool(compiler, 'clang++', self.compiler_version)[0]
        elif compiler == 'gcc':
            return Toolchain.find_compiler_tool(compiler, 'g++', self.compiler_version)[0]
        # msvc can compile with cl.exe regardless of language
        return self.compiler_path()

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name

    @staticmethod
    def find_gcc_tool(name, version=None):
        """ Finds gcc, gcc-ld, gcc-ranlib, etc at a specific version, or the latest one available """
        versions = [version] if version else _gcc_versions()
        return _find_compiler_tool(name, versions)

    @staticmethod
    def find_llvm_tool(name, version=None):
        """ Finds clang, clang-tidy, lld, etc at a specific version, or the
        latest one available """
        versions = [version] if version else _clang_versions()
        return _find_compiler_tool(name, versions)

    @staticmethod
    def find_apple_llvm_compiler(name, version=None):
        """ Finds apple versions of clang compilers at a specific version, or the latest one available.
            Note: apple does not ship tools like clang-tidy, so if installed
            those will follow regular llvm versioning """
        versions = [version] if version else _appleclang_versions()
        return _find_compiler_tool(name, versions)

    @staticmethod
    def find_msvc(version=None):
        """ Finds MSVC at a specific version, or the latest one available """
        def _find_msvc(version, install_vswhere=True):
            vswhere = util.where('vswhere')
            # if that fails, install vswhere and try again
            if not vswhere and install_vswhere:
                result = util.run_command(
                    'choco', 'install', '--no-progress', 'vswhere')
                if result.returncode == 0:
                    return _find_msvc(version, False)
                return None, None

            # A Visual Studio installation might have toolsets available for compiling
            # earlier versions. vswhere doesn't know about these toolsets, so
            # we'll just assume that any installation >= version can do the job.
            #
            # vswhere's -version flag expects a range, and if you just pass
            # a single int you'll get told about anything >= version.
            # Perfect, exactly what we want.
            result = util.run_command('vswhere', '-legacy', '-version', version,
                                      '-property', 'installationPath', '-sort', quiet=True)
            installations = result.output.splitlines()
            if installations:
                return installations[0], version
            return None, None

        versions = [version] if version else _msvc_versions()
        for version in versions:
            path, version = _find_msvc(version)
            if path:
                return path, version
        return None, None

    @staticmethod
    def find_compiler(compiler, version=None):
        """ Returns path, found_version for the requested compiler if it is installed """
        if compiler == 'clang':
            if current_os() == "macos":
                return Toolchain.find_apple_llvm_compiler(compiler, version)
            else:
                return Toolchain.find_llvm_tool(compiler, version)
        elif compiler == 'appleclang':
            return Toolchain.find_apple_llvm_compiler('clang', version)
        elif compiler == 'gcc':
            return Toolchain.find_gcc_tool(compiler, version)
        elif compiler == 'msvc':
            return Toolchain.find_msvc(version)
        return None, None

    @staticmethod
    def find_compiler_tool(compiler, tool, version=None):
        """ Returns path, found_version for the requested tool if it is installed """
        if compiler == 'clang':
            return Toolchain.find_llvm_tool(tool, version)
        elif compiler == 'appleclang':
            return Toolchain.find_apple_llvm_compiler(tool, version)
        elif compiler == 'gcc':
            return Toolchain.find_gcc_tool(tool, version)

        return None, None

    @staticmethod
    def all_compilers():
        """ Returns a list of tuples of all available (compiler, version) """
        compilers = []
        for version in _gcc_versions():
            path, _version = Toolchain.find_gcc_tool('gcc', version)
            if path:
                compilers.append(('gcc', version))

        is_mac = current_os() == "macos"
        versions = _appleclang_versions() if is_mac else _clang_versions()
        for version in versions:
            path, _version = Toolchain.find_llvm_tool('clang', version)
            if path:
                compilers.append(('appleclang' if is_mac else 'clang', version))
        if current_os() == 'windows':
            for version in _msvc_versions():
                path, _version = Toolchain.find_msvc(version)
                if path:
                    compilers.append(('msvc', version))
        return compilers

    _default_compiler = None
    _default_version = None

    @staticmethod
    def default_compiler(target=None, arch=None):
        """ Finds the system default compiler and returns (compiler, version) """
        if Toolchain._default_compiler and Toolchain._default_version:
            return Toolchain._default_compiler, Toolchain._default_version

        if target and arch and _is_cross_compile(target, arch):
            return 'gcc', '4.8'

        def _find_compiler():
            compiler = None
            version = None
            platform = current_os()

            if platform == 'windows':
                compiler = 'msvc'
                version = Toolchain.find_msvc()[1]
            else:
                # resolve CC and /usr/bin/cc
                for env_cc in (util.where(os.environ.get('CC', None)), util.where('cc')):
                    if env_cc:
                        cc, ccver = _compiler_version(env_cc)
                        if cc and ccver:
                            return cc, ccver

                # Try to find clang or gcc
                clang_path, clang_version = Toolchain.find_llvm_tool('clang')
                print(clang_path)
                gcc_path, gcc_version = Toolchain.find_gcc_tool('gcc')
                if clang_path:
                    compiler = 'clang'
                    version = clang_version
                elif gcc_path:
                    compiler = 'gcc'
                    version = gcc_version
                else:
                    print(
                        'Neither GCC or Clang could be found on this system, perhaps not installed yet?')

            if not compiler or not version:
                print('WARNING: Default compiler could not be found')

            print('Default Compiler: {} {}'.format(compiler, version))
            return compiler, version

        Toolchain._default_compiler, Toolchain._default_version = _find_compiler()
        return Toolchain._default_compiler, Toolchain._default_version

    @staticmethod
    def is_compiler_installed(compiler, version):
        """ Returns True if the specified compiler is already installed, False otherwise """
        compiler_path, found_version = Toolchain.find_compiler(
            compiler, version)
        return compiler_path != None
