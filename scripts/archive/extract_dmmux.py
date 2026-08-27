import pypdf

pdf_path = r"D:\STM\work\dcl-controller\STM32H723 docs\rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"

reader = pypdf.PdfReader(pdf_path)

# DMAMUX pages
pages = [683, 684, 685, 686, 687, 688]

for pg in pages:
    idx = pg - 1
    if idx < len(reader.pages):
        text = reader.pages[idx].extract_text()
        if text:
            print(f"\n=== PAGE {pg} ===")
            print(text[:2500])
