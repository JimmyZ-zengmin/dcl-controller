import asyncio, json, websockets, os, sys, time, struct

# Add compiler path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'compiler'))
os.chdir(os.path.join(os.path.dirname(__file__), '..'))

from pyocd.core.helpers import ConnectHelper

# Read binary
from dcl_compiler import DCLCompiler
src = open('compiler/reactor_control.dcl', encoding='utf-8').read()
c = DCLCompiler(); c.parse(src); c.topological_sort(); c.validate_resources()
binary = c.generate_binary()

n_routes, n_params, _ = struct.unpack_from('<III', binary, 0)
off = 12
rb = binary[off:off + n_routes * 16]; off += n_routes * 16
pb = binary[off:off + n_params * 16]

print(f'n_routes={n_routes}, n_params={n_params}')

# Deploy: halt CPU, write routes + DEPLOYED_MAGIC, resume (no reset)
with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    t.reset_and_halt()
    
    # Clear and write
    t.write_memory_block8(0x20001710, bytes(1024 * 16))
    t.write_memory_block8(0x20005710, bytes(512 * 16))
    t.write_memory_block8(0x20001710, bytes(rb))
    t.write_memory_block8(0x20005710, bytes(pb))
    t.write32(0x20000040, n_routes)
    
    # Write DEPLOYED_MAGIC to DTCM+0x100 (SCRATCH[2]) to prevent firmware init from overwriting
    # main.c:1738 checks: if (SCRATCH[2] != DEPLOYED_MAGIC) { init hardcoded 49 routes }
    # Without this, ANY reset (IWDG ~520ms) re-runs startup code and restores flash-default table
    t.write32(0x200000F8, 0xDEADBEEF)
    print(f'Wrote DEPLOYED_MAGIC to DTCM+0x0F8 (SCRATCH2)')
    
    rb_check = t.read_memory_block8(0x20001710, 32)
    print(f'After write: {bytes(rb_check).hex()}')
    print(f'N_ROUTES readback: {t.read32(0x20000040)}')
    print(f'SCRATCH2 readback: {hex(t.read32(0x200000F8))}')
    
    # Resume without reset
    t.resume()

time.sleep(1.0)

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    n_final = t.read32(0x20000040)
    scratch = t.read32(0x200000F8)
    print(f'N_ROUTES after 1s: {n_final}')
    print(f'SCRATCH[2] after 1s: {hex(scratch)}')
    
    # Now test: does reset preserve the deployed table?
    print('Testing reset...')
    t.reset()

time.sleep(1.0)

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    n_after_reset = t.read32(0x20000040)
    scratch_after = t.read32(0x200000F8)
    raw_after = t.read_memory_block8(0x20001710, 32)
    print(f'N_ROUTES after reset: {n_after_reset}')
    print(f'SCRATCH[2] after reset: {hex(scratch_after)}')
    print(f'Route table after reset: {bytes(raw_after).hex()}')
    
    full_rb = t.read_memory_block8(0x20001710, len(rb))
    print(f'Route table matches: {bytes(full_rb) == bytes(rb)}')
