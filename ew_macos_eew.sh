# Note: Apple Xcode 8.2.1 is the final version that supports 32-bit builds:
#    ld: warning: The i386 architecture is deprecated for macOS (remove from the
#    Xcode build setting: ARCHS)

# Enable USE_CC_BITS to default EW_BITs to the compiler target (removes -m32/64)
# Otherwise, the default is 32, as it was. 'true' should get you 64 bit on a 64
# bit system.

USE_CC_BITS=true

# Create an Earthworm environment on macOS
# This file should be sourced by a Bourne shell wanting
# to run or build an EARTHWORM system under macOS.

# For running EW on a Mac, if any ports are used, make sure that your
# selected ports for wave_serverV or exports are below the range specified
# by your kernel for dynamic port allocation 
# (see sysctl net.inet.ip.portrange.{first,last})

# Set environment variables describing your Earthworm directory/version

# Use value from elsewhere IF defined (eg from .bashrc)
# otherwise use the value after the :-
#export EW_HOME="${EW_INSTALL_HOME:-/opt/earthworm}"
#export EW_VERSION="${EW_INSTALL_VERSION:-earthworm_8.0}"
#EW_RUN_DIR="${EW_RUN_DIR:-${EW_HOME}/run_working}"
# Or set your own values directly
export EW_HOME=/Users/dayichen/Earthworm
export EW_VERSION=earthworm_8.0
EW_RUN_DIR=$EW_HOME/run_eew

# This next env var is required if you run statmgr:
export SYS_NAME=`hostname`

# Set environment variables used by Earthworm modules at run-time
# Path names must end with the slash "/"
export EW_INSTALLATION="${EW_INSTALL_INSTALLATION:-INST_UNKNOWN}"
export EW_PARAMS="${EW_RUN_DIR}/params/"
export EW_LOG="${EW_RUN_DIR}/logs/"
export EW_DATA_DIR="${EW_RUN_DIR}/data/"

# Tack the Earthworm bin directory in front of the current path
export PATH="${EW_HOME}/${EW_VERSION}/bin:${PATH}"

# Set environment variables for compiling Earthworm modules

# Print directory entry/exit message, as on Linux
export MAKEFLAGS=--print-directory

# Be explicit about which compilers to use (only CC is checked for validity)
export CC=gcc
export CXX=g++

if [ "${CC}" = "gcc" ] ; then

   # Set EW_BITS=64 to build for 64-bit target (note that with EW_BITS=64
   # size of 'long' type changes from 4 bytes to 8 bytes)

   if [ -n "${USE_CC_BITS}" ] ; then
      CC_BITS=32
      if [[ `${CC} -dumpmachine` == *64-* ]] ; then
         CC_BITS=64
      fi
      if [ -n "${EW_INSTALL_BITS}" ] ; then
         export EW_BITS="${EW_INSTALL_BITS}"
         TARGET="-m${EW_BITS}"
      else
         export EW_BITS="${CC_BITS}"
         TARGET=""
      fi
   else
      export EW_BITS="${EW_INSTALL_BITS:-32}"
      TARGET="-m${EW_BITS}"
   fi

   # Warning flags for compiler:
   WARNFLAGS="-Wall -Wextra -Wno-sign-compare -Wno-unused-parameter -Wno-unknown-pragmas -Wno-pragmas -Wformat -Wdeclaration-after-statement"
   # Extra flags for enabling more warnings during code development:
   #WARNFLAGS="-Wall -Wextra -Wcast-align -Wpointer-arith -Wbad-function-cast -Winline -Wundef -Wnested-externs -Wshadow -Wfloat-equal -Wno-unused-parameter -Wformat -Wdeclaration-after-statement"

   # -D_DARWIN_USE_64_BIT_INODE (OSX 10.5 and later) is for 64-bit inode numbers (see man 2 stat)

   # For some Apple gcc/llvm, -pthread does not behave like gcc -pthread:
   # it is a valid flag when compiling, but complains when linking:
   #    clang: warning: argument unused during compilation: '-pthread'
   # Combined compiling/linking does not complain.  For example, Apple LLVM
   # version 8.0.0 (clang-800.0.42.1) [Xcode 8.2.1] on El Capitan complains,
   # while Apple LLVM version 10.0.0 (clang-1000.11.45.5) [Xcode 8.2.1] on
   # High Sierra does not.

   # gcc -pthread compiles and links with POSIX Threads support

   # Set initial defaults for the gmake implicit .c.o and .cpp.o target rules

   # C compiler flags (also used for ld flags)
   export CFLAGS="-D_MACOSX ${TARGET} -g -pthread ${WARNFLAGS}"
   # C++ compiler flags
   export CXXFLAGS="${CFLAGS}"
   # C preprocessor defs and includes
   export CPPFLAGS="-D_MACOSX -D_DARWIN_C_SOURCE -D_INTEL -D_USE_SCHED -pthread -D_USE_PTHREADS -D_DARWIN_USE_64_BIT_INODE -I${EW_HOME}/${EW_VERSION}/include"

   # This is needed for the linker to use our libutil instead of the macOS libutil
   export LDFLAGS=-Wl,-search_paths_first

   # Earthworm makefiles override CFLAGS=$(GLOBALFLAGS) (for now), and not all
   # makefiles have been converted yet to use the implicit gmake target rules
   export GLOBALFLAGS="${CFLAGS} ${CPPFLAGS}"

   # macOS uses the same makefile.unix options as Linux
   export PLATFORM="LINUX"

else

   echo "CC must be gcc"

fi

# Pick a Fortran compiler
# gfortran (freeware, available from http://hpc.sourceforge.net)
export FC=gfortran
# Intel Fortran (requires a paid license)
#export FC=ifort

if [ "${FC}" = "gfortran" ] ; then

   # Extra run-time checks: -fcheck=all
   #export FFLAGS="${TARGET} -O -g -Wuninitialized -Werror=line-truncation -ffpe-trap=invalid,zero,overflow -fcheck=all,no-array-temps -fbacktrace"
   export FFLAGS="${TARGET} -O -g -Wuninitialized -Werror=line-truncation -ffpe-trap=invalid,zero,overflow -fbacktrace"

   # FC_MAIN_IS_C is not needed with gfortran
   #export FC_MAIN_IS_C=-nofor-main

elif [ "${FC}" = "ifort" ] ; then

   # Extra run-time checks: -check bounds,uninit
   # ifort version 15 introduced the -init option; older compilers complain, but ignore it
   # ifort complains about comments past column 72; -warn truncated_source is pretty useless
   #export FFLAGS="${TARGET} -g -O3 -init=snan -init=arrays -extend-source -warn truncated_source -fpe-all=0 -check bounds,uninit -diag-disable 8290 -traceback"
   export FFLAGS="${TARGET} -g -O3 -init=snan -init=arrays -extend-source -warn truncated_source -fpe-all=0 -diag-disable 8290 -traceback"

   # Intel Fortran supplies main() unless told not to
   export FC_MAIN_IS_C=-nofor-main

else

   echo "FC must be either gfortran or ifort"

fi

# Alternatively, you can hard-code values here:
#export FC='...'
#export FFLAGS='...'
