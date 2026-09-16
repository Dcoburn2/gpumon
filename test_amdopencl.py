"""Genuinely load the AMD V620 with an OpenCL kernel and watch ADL readings move.

This is the decisive test for the AMD sensor path: if temperature, hotspot, power
and clocks all climb while a compute kernel runs on the card, the readings are
real and correctly attributed. It also proves the two cards are read separately,
because each is loaded in turn.
"""
from __future__ import annotations

import ctypes
import time

import metrics as M

cl = ctypes.WinDLL("OpenCL.dll")

clGetPlatformIDs = cl.clGetPlatformIDs
clGetPlatformIDs.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
clGetDeviceIDs = cl.clGetDeviceIDs
clGetDeviceIDs.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_uint,
                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
clGetDeviceInfo = cl.clGetDeviceInfo
clGetDeviceInfo.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                            ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
clCreateContext = cl.clCreateContext
clCreateContext.restype = ctypes.c_void_p
clCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                            ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_int)]
clCreateCommandQueue = cl.clCreateCommandQueue
clCreateCommandQueue.restype = ctypes.c_void_p
clCreateCommandQueue.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_ulonglong, ctypes.POINTER(ctypes.c_int)]
clCreateProgramWithSource = cl.clCreateProgramWithSource
clCreateProgramWithSource.restype = ctypes.c_void_p
clCreateProgramWithSource.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.POINTER(ctypes.c_char_p),
                                      ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
clBuildProgram = cl.clBuildProgram
clBuildProgram.restype = ctypes.c_int
clBuildProgram.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                           ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
clCreateBuffer = cl.clCreateBuffer
clCreateBuffer.restype = ctypes.c_void_p
clCreateBuffer.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_size_t,
                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
clCreateKernel = cl.clCreateKernel
clCreateKernel.restype = ctypes.c_void_p
clCreateKernel.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
clSetKernelArg = cl.clSetKernelArg
clSetKernelArg.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                           ctypes.c_void_p]
clEnqueueNDRangeKernel = cl.clEnqueueNDRangeKernel
clEnqueueNDRangeKernel.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
    ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
clFinish = cl.clFinish
clFinish.argtypes = [ctypes.c_void_p]
clReleaseMemObject = cl.clReleaseMemObject
clReleaseMemObject.argtypes = [ctypes.c_void_p]
clReleaseKernel = cl.clReleaseKernel
clReleaseKernel.argtypes = [ctypes.c_void_p]
clReleaseProgram = cl.clReleaseProgram
clReleaseProgram.argtypes = [ctypes.c_void_p]
clReleaseCommandQueue = cl.clReleaseCommandQueue
clReleaseCommandQueue.argtypes = [ctypes.c_void_p]
clReleaseContext = cl.clReleaseContext
clReleaseContext.argtypes = [ctypes.c_void_p]

CL_DEVICE_TYPE_GPU = 1 << 2
CL_DEVICE_NAME = 0x102B
CL_DEVICE_VENDOR = 0x102C
CL_MEM_READ_WRITE = 1 << 0

# clGetDeviceIDs takes cl_device_id*, so each element is itself a pointer.
CL_DEVICE_ID = ctypes.c_void_p

KERNEL = b"""
__kernel void burn(__global float *out, const float seed) {
    size_t gid = get_global_id(0);
    float x = seed + (float)(gid & 1023);
    for (int i = 0; i < 20000; i++) {
        x = native_sqrt(x * x + 1.0001f) + native_sin(x) * 0.0001f;
    }
    out[gid] = x;
}
"""


def device_name(dev: ctypes.c_void_p) -> str:
    buf = ctypes.create_string_buffer(256)
    size = ctypes.c_size_t(0)
    clGetDeviceInfo(dev, CL_DEVICE_NAME, 256, buf, ctypes.byref(size))
    return buf.value.decode("utf-8", "replace")


def list_gpu_devices() -> list[tuple[int, str, int]]:
    platforms = (ctypes.c_void_p * 8)()
    count = ctypes.c_uint(0)
    if clGetPlatformIDs(8, ctypes.cast(platforms, ctypes.c_void_p),
                        ctypes.byref(count)) != 0:
        return []
    out = []
    for p in range(count.value):
        devices = (CL_DEVICE_ID * 16)()
        ndev = ctypes.c_uint(0)
        rc = clGetDeviceIDs(platforms[p], CL_DEVICE_TYPE_GPU, 16,
                            ctypes.cast(devices, ctypes.c_void_p),
                            ctypes.byref(ndev))
        if rc != 0:
            continue
        for d in range(ndev.value):
            out.append((devices[d] or 0, device_name(devices[d]), p))
    return out


print("=" * 88)
print("AMD GPU LOAD TEST (OpenCL compute kernel)")
print("=" * 88)
devices = list_gpu_devices()
print(f"\n{len(devices)} OpenCL GPU device(s):")
for dev, name, plat in devices:
    print(f"  {name}   (platform {plat})")
if not devices:
    print("no OpenCL GPU devices; cannot load the AMD cards")
    raise SystemExit(0)

sm = M.SensorManager()
sm.prime()
time.sleep(1.0)
amd = [g for g in sm.gpus if "adl" in g.sources]
print(f"\nADL-monitored cards: {[g.display_name for g in amd]}")

KEYS = ("temp", "hotspot", "mem_temp", "power", "clock_core", "clock_mem",
        "fan", "fan_rpm", "util")


def snap() -> dict[str, dict[str, float]]:
    values = sm.poll()
    return {g.pci: {k: values.get(f"gpu{g.index}_{k}") for k in KEYS} for g in amd}


def load_device(handle, seconds: float) -> None:
    """Run a compute kernel on the device whose cl_device_id is `handle`."""
    dev = CL_DEVICE_ID(handle)
    err = ctypes.c_int(0)
    one = ctypes.c_uint(1)
    devices = (CL_DEVICE_ID * 1)(dev)
    ctx = clCreateContext(None, 1, ctypes.cast(devices, ctypes.c_void_p),
                          None, None, ctypes.byref(err))
    if not ctx:
        print(f"    clCreateContext failed (err {err.value})")
        return
    queue = clCreateCommandQueue(ctx, dev, 0, ctypes.byref(err))
    if not queue:
        print(f"    clCreateCommandQueue failed (err {err.value})")
        clReleaseContext(ctx)
        return
    src = ctypes.c_char_p(KERNEL)
    program = clCreateProgramWithSource(ctx, 1, ctypes.byref(src), None,
                                        ctypes.byref(err))
    build = clBuildProgram(program, 1, ctypes.cast(devices, ctypes.c_void_p),
                           None, None, None)
    if build != 0:
        log = ctypes.create_string_buffer(8192)
        cl.clGetProgramBuildInfo(program, dev, 0x1183, 8192, log, None)
        print(f"    kernel build failed ({build}): "
              f"{log.value.decode('utf-8','replace')[:300]}")
        clReleaseProgram(program)
        clReleaseCommandQueue(queue)
        clReleaseContext(ctx)
        return
    kernel = clCreateKernel(program, b"burn", ctypes.byref(err))
    if not kernel:
        print(f"    clCreateKernel failed (err {err.value})")
        clReleaseProgram(program)
        clReleaseCommandQueue(queue)
        clReleaseContext(ctx)
        return
    n = 1 << 20
    buf = clCreateBuffer(ctx, CL_MEM_READ_WRITE, n * 4, None, ctypes.byref(err))
    # cl_mem is an opaque pointer, so arg_size is the size of the pointer and
    # ctypes needs a pointer object (not a raw int) to take its address.
    mem_arg = CL_DEVICE_ID(buf or 0)
    clSetKernelArg(kernel, 0, ctypes.sizeof(CL_DEVICE_ID), ctypes.byref(mem_arg))
    seed = ctypes.c_float(1.0)
    clSetKernelArg(kernel, 1, ctypes.sizeof(ctypes.c_float), ctypes.byref(seed))
    global_size = ctypes.c_size_t(n)
    local_size = ctypes.c_size_t(64)
    deadline = time.time() + seconds
    launches = 0
    while time.time() < deadline:
        rc = clEnqueueNDRangeKernel(queue, kernel, 1, None,
                                    ctypes.byref(global_size),
                                    ctypes.byref(local_size), 0, None, None)
        if rc != 0:
            print(f"    clEnqueueNDRangeKernel failed (err {rc})")
            break
        clFinish(queue)
        launches += 1
    print(f"    {launches} kernel launches completed")
    clReleaseMemObject(buf)
    clReleaseKernel(kernel)
    clReleaseProgram(program)
    clReleaseCommandQueue(queue)
    clReleaseContext(ctx)


for handle, name, _plat in devices:
    if "v620" not in name.lower() and "amd" not in name.lower() and \
            "gfx" not in name.lower():
        print(f"\nskipping {name} (not an AMD device)")
        continue
    print(f"\n{'-' * 88}")
    print(f"loading {name} for 20 s")
    print(f"{'-' * 88}")
    before = snap()
    print(f"  before: " + " | ".join(
        f"{pci}: temp={b.get('temp')} hotspot={b.get('hotspot')} "
        f"power={b.get('power')} clk={b.get('clock_core')} util={b.get('util')}"
        for pci, b in before.items()))

    import threading
    worker = threading.Thread(target=load_device, args=(handle, 20.0), daemon=True)
    worker.start()

    peak = {pci: dict(vals) for pci, vals in before.items()}
    for tick in range(20):
        now = snap()
        line = []
        for pci, fields in now.items():
            line.append(f"{pci}: temp={fields.get('temp')} "
                        f"hs={fields.get('hotspot')} pwr={fields.get('power')} "
                        f"clk={fields.get('clock_core')} util={fields.get('util')}")
            for k, v in fields.items():
                if v is not None:
                    peak[pci][k] = max(peak[pci].get(k, float("-inf")), v)
        print(f"    t={tick:2}s  " + " | ".join(line))
        time.sleep(1.0)
    worker.join(timeout=30)

    print(f"  peak during load:")
    for pci, fields in peak.items():
        delta_temp = None
        if before[pci].get("temp") is not None and fields.get("temp") is not None:
            delta_temp = fields["temp"] - before[pci]["temp"]
        print(f"    {pci}: temp {before[pci].get('temp')} -> {fields.get('temp')} "
              f"(+{delta_temp}) hotspot -> {fields.get('hotspot')} "
              f"power -> {fields.get('power')} clk -> {fields.get('clock_core')} "
              f"util -> {fields.get('util')}")
    time.sleep(8)

sm.close()
print("\ndone")
