import pypdf

pdf_path = r"D:\STM\work\dcl-controller\STM32H723 docs\rm0468-stm32h723733-stm32h725735-and-stm32h730-value-line-advanced-armbased-32bit-mcus-stmicroelectronics.pdf"

reader = pypdf.PdfReader(pdf_path)

# Extract specific pages
pages_of_interest = [434, 435, 436, 637, 638, 639, 640, 3319, 3320, 3321, 3322]

for pg_num in pages_of_interest:
    idx = pg_num - 1
    if idx < len(reader.pages):
        text = reader.pages[idx].extract_text()
        if text:
            print(f"\n{'='*60}")
            print(f"=== PAGE {pg_num} ===")
            print(f"{'='*60}")
            print(text[:3000])
