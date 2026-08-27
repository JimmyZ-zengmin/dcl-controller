import pypdf

pdf_path = r"D:\STM\work\dcl-controller\STM32H723 docs\rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"

reader = pypdf.PdfReader(pdf_path)
print(f"Total pages: {len(reader.pages)}")

keywords = ["AHB1ENR", "DMAMUX1EN", "DMA request", "DMAMUX", "SxCR", "EN bit", "RCC_AHB1"]

for i, page in enumerate(reader.pages):
    text = page.extract_text()
    if text:
        for kw in keywords:
            if kw in text:
                print(f"\n=== Page {i+1} (keyword: {kw}) ===")
                # Print lines containing the keyword
                for line in text.split('\n'):
                    if kw in line:
                        print(line.strip())
                break
