.PHONY: all clean

all: main.pdf

main.pdf: main.tex refs.bib
	pdflatex -interaction=nonstopmode -halt-on-error main.tex
	biber main || true
	pdflatex -interaction=nonstopmode -halt-on-error main.tex
	pdflatex -interaction=nonstopmode -halt-on-error main.tex

clean:
	rm -f *.aux *.log *.bbl *.bcf *.blg *.out *.toc *.run.xml *.fls *.fdb_latexmk
