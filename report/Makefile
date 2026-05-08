.PHONY: all clean

TEX_SOURCES := $(shell find . -name '*.tex')

all: main.pdf

main.pdf: $(TEX_SOURCES) refs.bib
	pdflatex -interaction=nonstopmode -halt-on-error main.tex
	biber main || true
	pdflatex -interaction=nonstopmode -halt-on-error main.tex
	pdflatex -interaction=nonstopmode -halt-on-error main.tex

clean:
	rm -f *.aux *.log *.bbl *.bcf *.blg *.out *.toc *.run.xml *.fls *.fdb_latexmk
