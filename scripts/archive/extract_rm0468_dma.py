import pypdf

pdf_path = r"D:\STM\work\dcl-controller\STM32H723 docs\rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"

reader = pypdf.PdfReader(pdf_path)

# Search for DMAMUX1EN across all pages
for i, page in enumerate(reader.pages):
    text = page.extract_text()
    if text and "DMAMUX1EN" in text:
        print(f"\n=== Page {i+1} ===")
        for line in text.split('\n'):
            if "DMAMUX1EN" in line or "DMAMUX1" in line:
                print(line.strip())

# Also search for "always on" or "no clock enable" related to DMAMUX
for i, page in enumerate(reader.pages):
    text = page.extract_text()
    if text and ("DMAMUX" in text and ("clock" in text.lower() or "enable" in text.lower() or "rst" in text.lower())):
        if i > 3300 and i < 3400:
            print(f"\n=== Page {i+1} (DMAMUX clock/enable) ===")
            for line in text.split('\n'):
                if "DMAMUX" in line or "clock" in line.lower():
                    print(line.strip())
