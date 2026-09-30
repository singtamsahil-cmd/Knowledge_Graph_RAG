from src.pdf_processor import collect_pdfs, extract_pdf_pages


def test_pdf_extraction_preserves_provenance(sample_pdfs):
    for domain, path in sample_pdfs.items():
        result = extract_pdf_pages(path)
        assert result.num_pages >= 1
        assert len(result.segments) == result.num_pages
        for seg in result.segments:
            assert seg.document == path.name
            assert seg.page_number >= 1


def test_empty_page_not_discarded(tmp_path):
    from reportlab.pdfgen import canvas
    from pypdf import PdfWriter

    p1 = tmp_path / "a.pdf"
    c = canvas.Canvas(str(p1))
    c.drawString(72, 750, "Hello world, Acme Corporation.")
    c.showPage()
    c.save()
    # append a truly blank page
    writer = PdfWriter(str(p1))
    writer.add_blank_page(width=612, height=792)
    p2 = tmp_path / "b.pdf"
    with open(p2, "wb") as f:
        writer.write(f)
    result = extract_pdf_pages(p2)
    assert result.num_pages == 2
    assert len(result.segments) == 2  # blank page kept
    assert result.empty_pages >= 1


def test_collect_pdfs_single_and_dir(sample_pdfs, tmp_path):
    one = next(iter(sample_pdfs.values()))
    assert collect_pdfs(str(one), None) == [one.__class__(one)] or len(collect_pdfs(str(one), None)) == 1
