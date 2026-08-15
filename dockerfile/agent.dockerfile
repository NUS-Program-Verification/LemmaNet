FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get upgrade -y
RUN apt-get install -y build-essential
RUN apt-get install -y software-properties-common

RUN apt install -y python3.11-dev python3.11-venv
RUN apt install -y --fix-missing \
    git graphviz libcairo2-dev libexpat1-dev libgtk-3-dev libgtksourceview-3.0-dev \
    zlib1g-dev adwaita-icon-theme-full curl libgmp-dev pkg-config bubblewrap

RUN apt install python3-pip -y

RUN apt update && apt install -y opam m4
RUN opam init -y --bare --disable-sandboxing
RUN opam switch create 4.14.0 ocaml-base-compiler.4.14.0 -y --jobs=1
RUN eval $(opam env) && opam install dune -y

RUN opam repo add coq-released https://coq.inria.fr/opam/released

# python2.7 is a declared depext of the opam dependency set; without it
# `opam switch import` stops to prompt and fails the build.
RUN apt-get install -y python2.7 vim

ENV PATH="/root/.opam/4.14.0/bin:$PATH"

# Let opam install any remaining system depexts itself instead of prompting,
# which it does even under `-y` and which is fatal in a non-interactive build.
ENV OPAMYES=1 \
    OPAMCONFIRMLEVEL=unsafe-yes

# Bust the cache when LEMMANET_REF changes, so rebuilds pick up new commits.
ARG LEMMANET_REF=main
RUN git clone --recursive https://github.com/NUS-Program-Verification/LemmaNet.git /LemmaNet \
    && cd /LemmaNet && git checkout ${LEMMANET_REF} \
    && git submodule update --init --recursive

WORKDIR /LemmaNet

# Rocq 8.18.0 and the rest of the opam dependency set.
RUN opam update && opam switch import deps.opam -y

RUN pip install -r requirement.txt

# Build the Frama-C/WP support library that the VCs depend on. The path
# /LemmaNet/benchmarks/AutoRocq-bench/libautorocq is what
# proof-search/configs/default_config.json already points at.
RUN make -C /LemmaNet/benchmarks/AutoRocq-bench/libautorocq

WORKDIR /LemmaNet/proof-search

CMD ["/bin/bash"]
