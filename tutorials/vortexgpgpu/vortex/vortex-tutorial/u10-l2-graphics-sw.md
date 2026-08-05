# 图形软件栈与软件发射器

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 Vortex 图形栈「双轨」设计的来龙去脉：一条是固定功能（FF，Fixed-Function）硬件快路径，另一条是跑在 SIMT 核上的设备侧软件回退（software fallback）。
- 理解 **gfx_ff_model** 这个主机侧「软件发射器」如何在 SimX 里模拟 RASTER/TEX/OM 三个 FF 单元，让图形测试在没有真实 FF 硬件时也能跑。
- 掌握贯穿全讲的工程核心——**单一真相来源（single source of truth）**：每像素的深度/模板测试、混合、纹理采样、光栅覆盖数学只写一份，主机 FF 模型与设备软件回退编译的是**同一份代码**，所以两者逐位一致。
- 读懂把 C++ 数学暴露给 Mesa（C 语言）驱动用的 C ABI 桥（`gfx_sw_abi`），以及它为何必须和 C++ 结构体逐字节对齐。

本讲承接 [u10-l1（图形硬件栈 RASTER/TEX/OM）](u10-l1-graphics-hw.md)：上一讲讲的是 FF 单元的**硬件**实现，本讲讲的是这些单元在**软件层**的镜像与回退——软件栈如何「伪装」出 FF，以及真没有 FF 时如何用 SIMT 核顶上。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 什么是「固定功能单元」与「软件发射器」

GPU 的图形流水线里，光栅化（RASTER）、纹理采样（TEX）、输出合并（OM/ROP）这三段通常是**固定功能硬件**——它们不是通用核跑的程序，而是专门连线算的电路。Vortex 也实现了这三个 FF 单元（见 u10-l1）。

但「固定功能」有个代价：硬件只能表达它被设计时支持的状态。比如 OM 硬件可能只写 `A8R8G8B8` 颜色 + `D24S8` 深度；遇到 `R8`、`RG8`、sRGB、MSAA 多采样等格式，硬件表达不了。

「软件发射器（software emitter）」就是用**软件**复刻这些固定功能行为的代码。Vortex 里有两类软件发射器：

- **主机侧 FF 模型**（`sw/common/gfx_ff_model.*`）：跑在主机 CPU 上，被 SimX 仿真器 include，用来在 RTL/硬件还没就绪时模拟 FF 行为，也是图形功能的「预言机（oracle）」。
- **设备侧软件回退**（`sw/gfx/`、`sw/common/gfx_sw.h`）：跑在 Vortex 的 SIMT 核上，当某个图形状态 FF 硬件表达不了时，由片段着色器（FS）内核转而调用软件实现。

### 2.2 为什么软件回退必须「设备常驻」

Vortex 图形栈的北极星目标是 **true GPU**：从 `submit`（提交绘制）到 `present`（呈现画面）之间，一切都在设备上完成、主机不介入。设计文档把这条原则说得很直白：

> 一旦某个状态 FF 单元无法表达，就用**设备侧 SIMT 软件回退**兜底——绝不走主机回路（never a host round trip）。

为什么不能像很多轻量驱动那样，遇到复杂状态就退回主机用 llvmpipe 软件渲染？因为「full residency（完全驻留）」 forbids（禁止）主机回退——一个真正的 GPU 不能动不动就把活儿扔回 CPU。所以「兜底的正确性路径」必须长在设备上。

### 2.3 「单一真相来源」为什么是关键

设想另一种设计：主机 FF 模型写一份混合（blend）数学，设备软件回退再写一份。两份代码迟早会漂移——主机说该是 `0x80`，设备算出 `0x7f`，于是 SimX 与 RTL 对不上，model_parity（见 u7-l4）崩盘。

Vortex 的解法很干净：把每像素的纯数学（比较、模板操作、混合因子、混合模式、纹理采样、覆盖行走）放进几个**无依赖、可独立编译（freestanding）**的头文件里，让主机 FF 模型和设备软件回退都 `#include` 它们。于是：

> 软件路径与 FF 路径逐位一致，**因为它们就是同一份代码**。

这句话是本讲的灵魂，请在脑子里给它画个重点。

## 3. 本讲源码地图

| 文件 | 作用 | 归属 |
|------|------|------|
| [docs/designs/graphics_software_stack.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md) | 图形软件栈总图：两棵树、两个边界、单一真相来源 | 设计文档 |
| [sw/common/gfx_ff_model.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.h) / [.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp) | 主机侧 FF 软件模型（RASTER/TEX/OM），被 SimX 消费 | `sw/common`（跨 sw/sim 共享） |
| [sw/common/gfx_sw.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h) | **单一真相头**：每像素 OM/纹理数学，主机与设备共享 | `sw/common` |
| [sw/common/gfx_sw_abi.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw_abi.h) / [sw/gfx/gfx_sw_abi.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_sw_abi.cpp) | 设备软件回退的 C ABI 桥（给 Mesa 的 C 驱动用） | ABI 在 `sw/common`，实现在 `sw/gfx` |
| [sw/gfx/libgfx_sw.mk](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/libgfx_sw.mk) | 设备软件回退的编译约定（divergence 闸门） | `sw/gfx` |
| [sw/gfx/gfx_frontend_k.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_frontend_k.h) | 设备侧图形前端内核（`expand_k`/`setup_k`/`binning_k`） | `sw/gfx` |
| [sw/runtime/include/graphics.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/graphics.h) / [common/graphics.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/graphics.cpp) | 主机驱动层：`Binning` 预言机、`DrawCommands`、FF 寄存器发射器 `program_*` | `sw/runtime` |

> **一个需要提醒你的出入**：设计文档 `graphics_software_stack.md` 把主机参考渲染器（golden oracle）指向 `sw/common/gfx_render.cpp`/`.h`。但在本讲的 HEAD（`d76b7f24e`）下，这两个文件**并不存在于磁盘**。实际承担「主机覆盖参考预言机」角色的是 `sw/runtime` 里的 `vortex::graphics::Binning`。本讲只引用真实存在的文件，对文档的过期描述会显式标注「待确认」。

## 4. 核心概念与源码讲解

### 4.1 软件栈双轨：FF 快路径与「单一真相」共享头

#### 4.1.1 概念说明

本模块建立全讲的「地图感」。Vortex 图形栈横跨**两棵代码树**：

- **`mesa_vortex`**（分支 `prism`）——Vulkan/Gallium **驱动**（名叫 vortexpipe），它消费 Vortex 当作一个 SDK。
- **本仓库**（`vortex`，分支 `prism`）——Vortex **平台**：SDK 软件（运行时 + 设备内核 + ABI）、SimX 模型、RTL。

驱动对平台的依赖是**单向**的：`mesa → Vortex`，就像一个用户态驱动消费 GPU SDK 那样。

绘制在设备上的完整流程（true-GPU 路径）是这样的：

```
 host submit ─► CP ─► expand_k ─► setup_k ──► binning_k ───► RASTER ──push──► FS ──► TEX ─► OM ─► framebuffer
                     (VS组装)    (裁剪+三角    (bin排序)       (FF:覆盖,     (SIMT:  (FF)  (FF)
                                  建立setup)                   early-Z,      vx_frag_load
                                                              packer,       → vx_tex4/vx_om4)
                                                              dispatch)
                     └───────────── sw/gfx 内核 ──────────────┘   └─── sim/simx 或 hw/rtl FF ───┘
     (任何 FF 表达不了的状态 → 设备侧 SIMT 软件回退: sw/gfx/libgfx_sw)
```

这里出现两个软件落点：

1. `sw/gfx` 的**前端内核**（`expand_k`/`setup_k`/`binning_k`）——在设备上做顶点组装、三角 setup、bin 排序，把活儿喂给 RASTER。
2. 当 FF 单元（RASTER/TEX/OM）表达不了某个状态时，FS 内核转而调用 `sw/gfx/libgfx_sw` 提供的**设备软件回退**。

#### 4.1.2 核心流程：两个边界与单一真相

设计文档把软件栈的纪律总结为「两个边界」加「一条真相线」。

**边界一：SDK 边界** —— Mesa 通过两个环境变量消费 Vortex SDK：`$VORTEX_PATH`（安装路径，取头文件和 `libvortex.so`）、`$VORTEX_HOME`（源码路径，取 `sw/gfx` 内核源码与内核工具链）。在线协议（on-wire ABI）`vx_gfx_abi.h` 是硬件契约，归 SDK 所有，所以它不会跟着驱动走。

**边界二：后端边界** —— 同一份 SDK + 内核，可在 **SimX**（仿真优先开发与性能评估）、**RTL**（300 MHz U55C 验收）或 **FPGA** 上运行，运行时由 `VORTEX_DRIVER` 选择。

**单一真相来源**（文档 §5）：设备内核只在一处存在——`sw/gfx/`（前端 + 软件回退）和 `sw/kernel/include/`（内联函数）。SimX 图形测试编译它们以验证 SimX 模型，vortexpipe 也编译**同样的文件**生成 `gfx_frontend.vxbin` 去启动它们。「无重复，无漂移（no duplication, no drift）」。

```
            ┌─────────── 每像素纯数学（一份） ───────────┐
            │  gfx_sw.h / gfx_frag_tex.h / gfx_frag_rast.h  │
            └──────────────────────────────────────────────┘
                  ▲ #include                        ▲ #include
                  │                                  │
        ┌─────────┴──────────┐              ┌───────┴────────────────┐
        │ 主机 FF 模型         │              │ 设备软件回退 libgfx_sw   │
        │ gfx_ff_model.cpp     │              │ (经 gfx_sw_abi.cpp 桥)   │
        │ 被 SimX 消费          │              │ 跑在 SIMT 核上            │
        └──────────────────────┘              └────────────────────────┘
```

#### 4.1.3 源码精读

设计文档把「true GPU」姿态讲得很清楚——主机只负责编译 shader、构建命令/状态块，之后一切都设备常驻：

> The host compiles shaders and builds a command/state block; the on-device front end … and the FF units … execute the whole draw over resident memory. The host `Binning()` / reference renderer is retained only as an **offline oracle**, not the runtime path. Where the FF units cannot represent a state, an **on-device SIMT software fallback** covers it — never a host round trip.

见 [docs/designs/graphics_software_stack.md:L31-L41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md#L31-L41)。

「两个边界」见 [docs/designs/graphics_software_stack.md:L201-L211](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md#L201-L211)，「单一真相来源」见 [docs/designs/graphics_software_stack.md:L212-L220](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md#L212-L220)——后者点明前端 ABI（`gfx_frontend_abi.h`）之所以留在 `sw/common`，是因为主机运行时的 `FrontEndPool` 也要 include 它。

#### 4.1.4 代码实践

**实践目标**：在仓库里验证「单一真相」不是空话——确认主机 FF 模型与设备软件回退确实 include 同一个头。

**操作步骤**：

1. 打开 [sw/common/gfx_ff_model.cpp:L14-L16](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L14-L16)，你会看到它 `#include "gfx_sw.h"`，注释写明这是「per-fragment OM ops 的单一真相来源（§7）」。
2. 打开 [sw/gfx/gfx_sw_abi.cpp:L19-L20](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_sw_abi.cpp#L19-L20)，设备回退的 C ABI 实现同样 `#include "gfx_sw.h"`。
3. 用下面的命令确认两边 include 的是同一个文件（注意搜索范围限定 `sw/`）。

```
grep -rn '#include "gfx_sw.h"' sw/
grep -rn '#include "gfx_frag_tex.h"' sw/ sim/
```

**需要观察的现象**：两条路径都指向 `sw/common/gfx_sw.h`（以及共享的 `gfx_frag_tex.h`、`gfx_frag_rast.h`）。

**预期结果**：主机 FF 模型（`gfx_ff_model.cpp`）、设备回退 C ABI（`gfx_sw_abi.cpp`）以及 MSAA 主机一致性测试（`tests/unittest/gfx_msaa`）都 include 同一组头——这就是「逐位一致因为就是同一份代码」的物理证据。

#### 4.1.5 小练习与答案

**练习 1**：为什么「软件回退」必须长在设备上，而不能遇到复杂状态就退回主机用 llvmpipe？

**参考答案**：因为 Vortex 的目标是 true GPU——从 submit 到 present 全程设备常驻、主机不介入（full residency forbids host fallback）。一旦允许主机回路，就不再是「真 GPU」姿态；而且主机回路会破坏 SimX↔RTL 的 model_parity（主机算的轨迹无法和设备退休指令对齐）。

**练习 2**：`mesa_vortex` 驱动消费 Vortex 平台用哪两个环境变量？依赖方向是哪边到哪边？

**参考答案**：`$VORTEX_PATH`（安装路径，取头文件 + `libvortex.so`）与 `$VORTEX_HOME`（源码路径，取 `sw/gfx` 内核源码 + 内核工具链）。依赖是单向的 `mesa → Vortex`，就像用户态驱动消费 GPU SDK。

---

### 4.2 主机侧 FF 软件模型 gfx_ff_model（SimX 的预言机）

#### 4.2.1 概念说明

`gfx_ff_model.{h,cpp}` 是 RASTER/TEX/OM 三个 FF 单元的**主机侧软件模型**。它的头文件注释一句话定位了它的身份：

> Host-side software models of the fixed-function TEX / OM / RASTER units. **Consumed by simx.**

它是「软件发射器」最直接的含义：在 SimX 仿真器里，FF 单元不是真的硬件电路，而是由这套 C++ 类模拟出来的。正因为它，图形测试在 RTL/真实 FF 硬件还没就绪时也能在 SimX 上跑起来。

它住在 `sw/common/` 而不是 `sim/` 下，因为它要**跨 sw 与 sim 共享**——头文件注释明确禁止它 include `sw/kernel/include/` 或 `sw/runtime/include/`（回忆 u2-l3 的边界纪律）。

#### 4.2.2 核心流程：四个模型类 + DCR 驱动配置

`gfx_ff_model` 暴露四个核心类，每个对应一段 FF 流水线：

| 类 | 模拟的 FF 单元 | 输入 | 行为 |
|----|---------------|------|------|
| `TextureSampler` | TEX 纹理采样 | `TexDCRS`（纹理寄存器状态）+ `(stage,u,v,lod)` | 算地址、取纹素、过滤、三线性 mip 混合 |
| `DepthTencil` | OM 的深度/模板测试 | `OMDCRS` + `(面, 深度, 旧深模值)` | 跑比较 + 模板操作，返回是否通过 |
| `Blender` | OM 的混合/逻辑操作 | `OMDCRS` + `(源色, 目标色)` | 算混合因子 + 混合模式，返回结果色 |
| `Rasterizer` | RASTER 覆盖行走 | `RasterDCRS` + 边方程 | 遍历瓦片产出覆盖的 2×2 quad |

它们的配置都由 **DCR（设备配置寄存器）**驱动——和真实硬件读同一批 `VX_DCR_*` 寄存器。每个 FF 单元在 SimX 里收到 DCR 写入时，调用对应类的 `configure(dcrs)` 把寄存器值解析成算法参数。

关键点：这些类的「重活」（比较、模板操作、混合因子）**自己不实现**，而是转发给 `gfx_sw::` 命名空间里的共享函数。`gfx_ff_model.cpp` 里有一组「瘦转发器（thin forwarders）」就是为了在不改动既有调用点的前提下，把数学路由到单一真相头。

#### 4.2.3 源码精读

头文件给四个模型类与三套 DCR 状态类（`RasterDCRS`/`OMDCRS`/`TexDCRS`）的定位见 [sw/common/gfx_ff_model.h:L14-L19](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.h#L14-L19)。其中 `TextureSampler` 把「地址/过滤描述符 + 采样数学」显式声明来自共享头：

```cpp
// The address/filter descriptor + the sampling math live in gfx_frag_tex.h (the
// single source of truth shared with the device SW fallback); alias it here so
// existing call sites (TextureSampler, tex_core) are unchanged.
using TexelRequest = gfx_tex::TexelRequest;
```

见 [sw/common/gfx_ff_model.h:L150](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.h#L150)。

`gfx_ff_model.cpp` 的 `TextureSampler::compute_request` 把请求直接委托给共享函数 `gfx_tex::tex_compute_request`，见 [sw/common/gfx_ff_model.cpp:L51-L64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L51-L64)，注释点明「采样数学与设备侧软件回退共享（gfx_frag_tex.h）」。`read()` 还实现了三线性 mip 混合（取两个相邻 mip 再按小数部分插值）。

「瘦转发器」是单一真相机制的直接证据——OM 的四个核心操作全部转发到 `gfx_sw::`：

```cpp
inline bool DoCompare(uint32_t func, uint32_t a, uint32_t b) {
  return gfx_sw::DoCompare(func, a, b);
}
inline ColorARGB DoBlendFunc(uint32_t func, ColorARGB src, ColorARGB dst, ColorARGB cst) {
  return gfx_sw::DoBlendFunc(func, src, dst, cst);
}
// …DoStencilOp / DoBlendMode 同理
```

见 [sw/common/gfx_ff_model.cpp:L91-L110](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L91-L110)。于是 `DepthTencil::test` 与 `Blender::blend` 调用的就是设备软件回退用的同一份比较/混合代码——见 [gfx_ff_model.cpp:L148-L180](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L148-L180)（深度/模板测试）与 [gfx_ff_model.cpp:L205-L219](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L205-L219)（混合）。

`Rasterizer::renderPrimitive` 同样把覆盖行走委托给共享的 `gfx_rast::rast_walk_primitive`，把自身的 `ShaderCB` 当作产出回调：

```cpp
gfx_rast::rast_walk_primitive(cfg, x, y, pid, edges,
  [&](uint32_t pos_mask, vortex::graphics::vec3e_t* bcoords, uint32_t prim_id) {
    shader_cb_(pos_mask, bcoords, prim_id, cb_arg_);
  });
```

见 [sw/common/gfx_ff_model.cpp:L241-L255](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L241-L255)。

**SimX 如何消费这套模型**？以 OM 为例，[sim/simx/om/om_core.cpp:L19](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/om/om_core.cpp#L19) include 了 `gfx_ff_model.h`，并在每次 DCR 写入时重新配置 `DepthTencil` 与 `Blender`：

```cpp
int dcr_write(uint32_t addr, uint32_t value) {
  dcrs_.write(addr, value);
  depth_stencil_.configure(dcrs_);
  blender_.configure(dcrs_);
  recompute_state();
  return 0;
}
```

见 [sim/simx/om/om_core.cpp:L137-L141](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/om/om_core.cpp#L137-L141)。在 COMPUTE 阶段，它对每条 lane 调用 `depth_stencil_.test(...)` 与 `blender_.blend(...)`——和真实硬件 OM 单元的数据通路一一对应（见 [om_core.cpp:L418-L440](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/om/om_core.cpp#L418-L440)）。`raster_core.cpp` 与 `tex_core.cpp` 同样 include `gfx_ff_model.h`，是另外两段的 SimX 镜像。

#### 4.2.4 代码实践

**实践目标**：验证「SimX 的 OM 单元行为 = `gfx_ff_model` 的 `DepthTencil`/`Blender` = `gfx_sw::` 共享数学」这条三级链。

**操作步骤**：

1. 阅读 [sim/simx/om/om_core.cpp:L137-L141](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/om/om_core.cpp#L137-L141)，确认 OM 收到 DCR 写入时调用了 `depth_stencil_.configure(dcrs_)` 与 `blender_.configure(dcrs_)`。
2. 跟到 [gfx_ff_model.cpp:L119-L146](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L119-L146)，看 `DepthTencil::configure` 如何从 `OMDCRS` 解析出深度函数、前后模板状态，并推导 `depth_enabled_`（深度函数非 ALWAYS 或写掩码开时才启用）。
3. 再跟到 [gfx_ff_model.cpp:L91-L110](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L91-L110)，确认 `DoCompare` 只是转发到 `gfx_sw::DoCompare`。

**需要观察的现象**：三个文件构成一条无分叉的调用链：`om_core → DepthTencil/Blender → gfx_sw::`。

**预期结果**：你能画出 `SimX OM 单元 ──configure/test/blend──► gfx_ff_model 类 ──forward──► gfx_sw:: 共享函数` 的依赖图，且这条链上没有任何「主机专用数学」分叉。

#### 4.2.5 小练习与答案

**练习 1**：`gfx_ff_model` 为什么放在 `sw/common/` 而不是 `sim/simx/`？

**参考答案**：因为它要跨 `sw` 与 `sim` 共享。它既被 SimX 的 `raster_core`/`om_core`/`tex_core` 消费，本身又依赖跨层共享的 ABI 类型（`vx_gfx_abi.h`）。按 u2-l3 的边界纪律，`sw/common` 是唯一合法的跨层共享通道（四层可访问、永不安装、不在守卫扫描范围内），所以放这里。

**练习 2**：为什么 `gfx_ff_model.cpp` 里要写一组「瘦转发器」把 `DoCompare` 等转发到 `gfx_sw::`，而不是直接调用？

**参考答案**：为了「不改既有调用点」。`DepthTencil::test` / `Blender::blend` 这些老接口原本调用本地的 `DoCompare`；引入单一真相机制时，最小侵入的做法是让本地 `DoCompare` 转发到共享的 `gfx_sw::DoCompare`，于是老调用点零改动，数学却已经统一到一份代码。

---

### 4.3 单一真相头 gfx_sw.h —— 设备与主机共享的 per-fragment 数学

#### 4.3.1 概念说明

[sw/common/gfx_sw.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h) 是本讲最关键的文件。它的角色由文件头注释点明：它是「设备可编译的 `libgfx_sw`」，**同时**被主机 FF 模型（`gfx_ff_model.cpp`）include，使得每像素数学只有一份真相——「软件路径与 FF 路径逐位一致，因为它们就是同一份代码」。

它必须满足两个硬约束：

1. **Freestanding（无依赖）**：不能 `#include <algorithm>` 或 `<cmath>`，因为裸金属设备（baremetal）上没有这些标准库。所以它自带 `sw_min`/`sw_max`。
2. **可被 SIMT 核编译**：内联进片段内核后，能扛住 Vortex 的 divergence pass（分支发散处理）。

#### 4.3.2 核心流程：状态结构 + 纯数学 + RMW 合并

`gfx_sw.h` 的内容可分三层。

**第一层：纯 per-fragment 操作（无状态、纯函数）**。每个都是 `switch` 把 FF 寄存器编码翻译成行为：

- `DoCompare(func, a, b)`：8 种深度/模板比较（NEVER/LESS/EQUAL/…/ALWAYS）。
- `DoStencilOp(op, ref, val)`：8 种模板操作（ZERO/REPLACE/INCR/…/KEEP）。
- `DoLogicOp`、`DoBlendFunc`、`DoBlendMode`：逻辑操作、混合因子、混合模式（ADD/SUB/MIN/MAX/LOGICOP）。

**第二层：OM 状态结构 `om_state_t`**。它把主机填进 OM DCR 的同一批值聚成一个 POD 结构，并用 `resolve_om_state()` 派生出 enable 标志与展开的颜色写掩码——派生方式与 FF 单元完全一致。例如 `blend_enabled` 当且仅当不是「ADD/ONE/ONE/ZERO/ZERO」这个恒等混合时才为真：

```cpp
s.blend_enabled = !((s.blend_mode_rgb == VX_OM_BLEND_MODE_ADD)
                 && (s.blend_mode_a   == VX_OM_BLEND_MODE_ADD)
                 && (s.blend_src_rgb  == VX_OM_BLEND_FUNC_ONE)
                 /* … */) ;
```

见 [gfx_sw.h:L179-L196](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L179-L196)。这条派生规则在 `gfx_ff_model.cpp` 的 `Blender::configure`/`DepthTencil::configure` 里有逐字对应的副本——两边各自从 DCR 推导出同样的 enable，再次印证「同一份真相」。

**第三层：一次 OM 读-改-写 `om_sample_rmw`**。这是整个软件 OM 的核心：读目标深度/模板 + 颜色 → 跑深度模板测试 + 混合 → 套写掩码 → 写回。它是 `om_fragment`（单采样）、`om_fragment_msaa`（每采样）、`om_fragment_mrt`（多渲染目标）三者的**共用函数体**，所以三条路径都与 FF OM 单元逐位一致。其流程是：

```
om_sample_rmw(s, z_addr, c_addr, face, src_color, src_depth):
  dbpp/cbpp = 按格式算字节宽
  ds_active = depth_enabled || stencil_enabled[face]
  need_c_read = color_write && (color_read || blend_enabled)
  dst_ds    = ds_active   ? om_load(z_addr, dbpp) : 0   // 读深模
  dst_color = om_decode_color(need_c_read ? om_load(c_addr,cbpp):0)  // 读色+解码
  ds_pass   = !ds_active || ds_test(s, face, src_depth, dst_ds, &merged)
  blended   = (blend_enabled && ds_pass) ? blend(s, src_color, dst_color) : src_color
  套写掩码 → om_store(z_addr, …) 与 om_store(c_addr, om_encode_color(…))   // 写回
  return ds_pass
```

注意它非原子——当每个像素只被一个片段触碰时正确；重叠片段的逐像素顺序是待定的确定性问题，由软件路径的瓦片串行化处理。`om_fragment` 见 [gfx_sw.h:L597-L602](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L597-L602)，`om_sample_rmw` 见 [gfx_sw.h:L349-L378](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L349-L378)。

文件还内建一个**软件纹理采样器**：`TexState` 结构（纹理 DCR 状态的设备镜像，见 [gfx_sw.h:L494-L505](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L494-L505)），`tex_sample_sw`（含三线性 mip 混合，见 [gfx_sw.h:L530-L533](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L530-L533)），以及数组纹理/立方体纹理变体。它的 per-LOD 采样走共享的 `gfx_tex::tex_compute_request` / `tex_apply_filter`，与主机 `TextureSampler` 同源。

#### 4.3.3 源码精读

文件头点明双重身份与「同一份代码」的承诺，见 [sw/common/gfx_sw.h:L14-L27](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L14-L27)：

```cpp
// The always-correct path that runs on the SIMT cores when a fixed-function
// unit cannot represent a required feature — full residency forbids a host
// (llvmpipe) fallback, so the completeness path lives on the device. This
// header is the device-compilable `libgfx_sw`; it is ALSO included by the host
// FF models (sw/common/gfx_ff_model.cpp) so the per-fragment math has a single
// source of truth — the SW path matches the FF path bit-for-bit because it
// IS the same code.
```

为保持 freestanding，文件自带 `sw_min`/`sw_max`（[gfx_sw.h:L43-L44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L43-L44)），且所有数学函数都标了 `__attribute__((always_inline))`——它们会被内联进片段内核。

一个必须强调的**编译期守卫**：完整的深度+模板+混合+逻辑操作合并会把片段内核的 CFG（控制流图）撑到很大，超过 Vortex divergence pass 默认的 100 个基本块（BB）上限。一旦超限，pass 会**静默跳过** StructurizeCFG + split/join，导致内核被错误编译（uniform 的 OM 状态读残留为不可选 marker、分支发散控制流未屏蔽）。修复是**编译开关**而非源码改动，所以文件用 `#error` 强制设备构建必须先定义 `GFX_SW_DIVERGENCE_OK`：

```cpp
#if defined(__VORTEX__) && !defined(GFX_SW_DIVERGENCE_OK)
#error "gfx_sw.h om_fragment needs the divergence-bbs build flag: include sw/gfx/libgfx_sw.mk …"
#endif
```

见 [gfx_sw.h:L574-L576](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L574-L576)。主机构建没有 divergence pass，所以不需要这个开关——这解释了为何主机 FF 模型与 MSAA 一致性测试能正常编译同一份合并代码。

#### 4.3.4 代码实践

**实践目标**：亲手验证「设备与主机跑的是同一份 OM 合并代码」——这是「软件发射器模拟 FF」之所以可信的根基。

**操作步骤**：

1. 打开 `tests/unittest/gfx_msaa/main.cpp` 的文件头注释 [tests/unittest/gfx_msaa/main.cpp:L1-L25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/unittest/gfx_msaa/main.cpp#L1-L25)。它会告诉你：这个主机单元测试「用设备跑的同一份代码」端到端演练 MSAA 软件路径（`gfx_rast::rast_sample_mask` 覆盖、`gfx_sw::om_fragment_msaa` 每采样 ROP、`msaa_*_addr` 存储、`msaa_resolve_color` 求解），并把结果对一个**独立预言机**核对。
2. 进入该目录构建并运行（待本地验证，依赖已配置好的 Vortex 工具链与 build 树）：

```
make -C tests/unittest/gfx_msaa run
```

**需要观察的现象**：测试应当报告所有像素通过——内部像素等于前景色、外部像素等于背景色、边缘像素是按覆盖样本数加权的混合（即抗锯齿）。第二条 pass 还会验证逐采样深度测试：一次被遮挡的重绘不应改变任何已覆盖样本。

**预期结果**：因为主机测试用的就是 `gfx_sw.h` 的 `om_fragment_msaa`/`msaa_*`，而这套函数也是设备 `libgfx_sw` 的实现，所以测试通过等于证明「设备将要跑的代码在主机上算出的结果与独立 oracle 一致」。若环境无法运行，明确标注「待本地验证」，并改为源码阅读型实践：对比 [gfx_sw.h:L179-L196](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L179-L196) 与 [gfx_ff_model.cpp:L197-L203](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L197-L203)，确认两边对 `blend_enabled` 的推导条件逐字相同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gfx_sw.h` 要自带 `sw_min`/`sw_max` 而不用 `std::min`/`std::max`？

**参考答案**：因为它必须 freestanding——要能被裸金属设备内核编译，而设备端没有 `<algorithm>` 标准 库。自带的 `sw_min`/`sw_max` 对此处整数用途与标准版本结果完全一致。

**练习 2**：`om_sample_rmw` 被注释为「非原子读-改-写」。它在什么前提下正确？重叠片段的顺序问题由谁解决？

**参考答案**：当每个（像素, 采样）只被一个片段触碰时正确（无并发同像素片段）。逐像素的顺序/原子性是「确定性问题（determinism open item）」，由软件路径的瓦片串行化（tile serialization）与 MSAA 工作编排来保证——即让 SIMT 调度不会把两个写同一像素的片段乱序交织。

---

### 4.4 设备软件回退的 C ABI 桥与编译约定

#### 4.4.1 概念说明

到这里有个现实问题：`gfx_sw.h` 是 C++，但 Mesa 的 vortexpipe 驱动把片段着色器从 NIR 编译成 LLVM IR、最终是 **C 调用约定**。C 驱动没法直接调 C++ 模板与命名空间函数。于是需要一座桥——`gfx_sw_abi`：

- **`sw/common/gfx_sw_abi.h`**：纯 C 头（`extern "C"`），定义一组 POD 描述符（`gfx_sw_texstate_t`/`gfx_sw_omstate_t`/`gfx_sw_omcolor_t`）和 C 入口函数（`gfx_tex_sample_sw`、`gfx_om_fragment_sw` 等），让 Mesa 的 C 驱动能直接 include、直接调。
- **`sw/gfx/gfx_sw_abi.cpp`**：C++ 实现，把 C 描述符 `reinterpret_cast` 成 `gfx_sw::` 的 C++ 结构体，再调单一真相头的数学。

它被编译成 LLVM bitcode（带 divergence 标志），供 vortexpipe 的 FS `llvm-link` 进来并内联。

#### 4.4.2 核心流程：POD 镜像 + static_assert 守卫

这座桥的合法性完全建立在「C ABI 描述符与 C++ 状态结构体逐字节布局一致」之上。因此实现里第一件事就是一组 `static_assert` 兜底：

```cpp
static_assert(sizeof(gfx_sw_texstate_t) == sizeof(gfx_sw::TexState),
              "gfx_sw_texstate_t must mirror gfx_sw::TexState");
static_assert(sizeof(gfx_sw_omstate_t)  == sizeof(gfx_sw::om_state_t),
              "gfx_sw_omstate_t must mirror gfx_sw::om_state_t");
static_assert(std::is_trivially_copyable<gfx_sw::TexState>::value &&
              std::is_trivially_copyable<gfx_sw::om_state_t>::value,
              "SW state structs must be POD for the C ABI");
```

这样 `reinterpret_cast` 才是良定义的，且主机与设备都能填同一种形式。

每个入口都很薄——校验覆盖位后转发：

```cpp
extern "C" void gfx_om_fragment_sw(const gfx_sw_omstate_t* st, uint32_t covered,
                                   uint32_t x, uint32_t y, uint32_t face,
                                   uint32_t color, uint32_t depth) {
  if (!covered) return;                  // 让 SIMT 调用方保持直流程序
  gfx_sw::om_fragment(*reinterpret_cast<const gfx_sw::om_state_t*>(st),
                      x, y, face, color, depth);
}
```

注意 `covered` 参数的设计意图：把「未覆盖片段丢弃」这个发散分支放进这个编译单元（在设备工具链的 divergence lowering 之下），让 FS 调用方保持直线程（straight-line），避免在 FS 里引入额外发散。

纹理入口 `gfx_tex_sample_sw` 直接转发 `gfx_sw::tex_sample_sw`（[gfx_sw_abi.cpp:L38-L41](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_sw_abi.cpp#L38-L41)）；数组纹理、立方体纹理、MRT 各有对应入口。

光栅化器还有一个特别的入口 `gfx_rast_walk_tile_sw`（[gfx_sw_abi.cpp:L75-L120](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_sw_abi.cpp#L75-L120)）：它**不用** `rast_walk_primitive` 的 Morton-DFS 递归（相邻瓦片覆盖不同会导致每 lane 递归深度发散，SIMT 重汇聚时丢片段），而是用固定 2×2-quad 叶网格 + 均匀循环计数，让所有 lane 锁步前进、产出完全相同的 quad。顺序不同（行主序 vs Morton），但 quad 间不重叠，所以合并图像一致——这是「为 SIMT 发散安全而改写算法、但不改结果」的典型范例。

#### 4.4.3 源码精读

C ABI 头的定位见 [sw/common/gfx_sw_abi.h:L14-L24](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw_abi.h#L14-L24)：vortexpipe 的 FS 在「某单元被路由到软件」时发出对这些入口点的调用；主机从绑定的流水线状态构建 POD 描述符，把它们的设备指针经内核参数传入。`gfx_om_fragment_sw` 的声明见 [gfx_sw_abi.h:L103-L105](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw_abi.h#L103-L105)，`gfx_sw_omstate_t` 描述符见 [gfx_sw_abi.h:L50-L66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw_abi.h#L50-L66)。

C ABI 实现的 `static_assert` 守卫见 [sw/gfx/gfx_sw_abi.cpp:L28-L36](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_sw_abi.cpp#L28-L36)，`gfx_om_fragment_sw` 的转发见 [gfx_sw_abi.cpp:L53-L60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_sw_abi.cpp#L53-L60)。

**编译约定 `libgfx_sw.mk`** 把 4.3 节那个 divergence 闸门封装成一个可复用片段，让任何设备片段内核都不会忘记提上限：

```makefile
LIBGFX_SW_VX_CFLAGS := -mllvm -vortex-divergence-max-bbs=512 -DGFX_SW_DIVERGENCE_OK
```

见 [sw/gfx/libgfx_sw.mk](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/libgfx_sw.mk)。用法是 `include` 它再把 `$(LIBGFX_SW_VX_CFLAGS)` 加进内核的 `VX_CFLAGS`——这同时定义了 `GFX_SW_DIVERGENCE_OK`（满足 `gfx_sw.h` 的 `#error`）并提了基本块上限到 512。

**前端内核**也住在这里。[sw/gfx/gfx_frontend_k.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_frontend_k.h) 定义三个设备内核入口：`expand_k`（VS 输出记录 → `setup_vertex_t[]`，见 [L66-L93](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_frontend_k.h#L66-L93)）、`setup_k`（裁剪 + 三角 setup → 稠密 primbuf + bbox，分 SETUP/SCAN/EMIT 三阶段，见 [L96-L174](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_frontend_k.h#L96-L174)）、`binning_k`（bin 排序 → tilebuf，分 BCOUNT/BSCAN/BEMIT/BHIST/BBASE/BSCATTER 六阶段，见 [L177-L300](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_frontend_k.h#L177-L300)）。它们用 `__kernel` + `__UNIFORM__` 注解（回忆 u4-l1 的多入口 `.vxbin` 与 uniform 标记），是「设备侧图形前端」的实体。

> 补一句对完整性的澄清：**FF 寄存器发射器**（`program_raster`/`program_om`/`program_tex`）住在 `sw/runtime/common/graphics.cpp`，它们把 `*_state_t` 翻译成 `VX_DCR_*` 寄存器写（见 [graphics.cpp:L501-L513](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/graphics.cpp#L501-L513)）。这是另一种「发射器」——发射的是硬件寄存器值，不是 per-fragment 数学。注意区分：本讲的主角是「FF 单元行为的软件模型/回退」，而 `program_*` 是「配置 FF 单元状态的寄存器打包器」，两者互补。

#### 4.4.4 代码实践

**实践目标**：理解为何 C ABI 桥不会悄悄出错——`static_assert` 在编译期就把布局漂移挡住。

**操作步骤**：

1. 读 [sw/gfx/gfx_sw_abi.cpp:L28-L36](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_sw_abi.cpp#L28-L36) 的三条 `static_assert`。
2. 对照 [sw/common/gfx_sw_abi.h:L50-L66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw_abi.h#L50-L66) 的 `gfx_sw_omstate_t`（C）与 [sw/common/gfx_sw.h:L154-L174](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L154-L174) 的 `gfx_sw::om_state_t`（C++），逐字段核对顺序与类型是否一致。
3. **思维实验（不改源码）**：假如有人在 C++ 的 `om_state_t` 里中间插了一个新字段，却忘了同步 C 描述符。问：会发生什么？何时报错？

**需要观察的现象**：两边字段顺序、类型逐一对齐；`is_trivially_copyable` 保证没有 vtable/虚函数，POD 可安全 `reinterpret_cast`。

**预期结果**：一旦布局不一致，`sizeof` 断言在编译 `gfx_sw_abi.cpp` 时立即失败（编译期错误，非运行期），指向「must mirror …」消息。这正是「靠编译器守住 ABI 一致」的工程手法——把契约变成不可绕过的编译期检查。思维实验的答案是：编译 `gfx_sw_abi.cpp` 时直接报错，而不是等到设备上算出错误像素。

#### 4.4.5 小练习与答案

**练习 1**：`gfx_om_fragment_sw` 多了一个 `covered` 参数，`om_fragment` 本身没有。为什么 ABI 层要加它？

**参考答案**：为了让 FS 调用方保持直线程。未覆盖片段的丢弃是个发散分支；把它放进 `gfx_sw_abi.cpp`（在设备 divergence lowering 之下）而非 FS 主体，FS 就不必自己处理「这个像素到底覆盖没」的发散逻辑，简化了着色器代码生成。

**练习 2**：`gfx_rast_walk_tile_sw` 为什么不直接复用 `rast_walk_primitive` 的 Morton-DFS 递归，而要改成固定 2×2-quad 叶网格 + 均匀循环？

**参考答案**：Morton-DFS 的递归深度依赖「该瓦片如何覆盖本图元」，相邻 lane 覆盖不同时递归深度会发散，SIMT 重汇聚时部分活跃的 warp 会丢片段。固定叶网格 + 均匀 trip 循环让所有 lane 锁步前进，产出完全相同的 quad 集合（只是顺序从 Morton 变成行主序）；因 quad 间不重叠，合并图像逐位一致。

---

## 5. 综合实践

把本讲四条主线串起来，完成下面这个「软件发射器全景追踪」任务。

**任务**：选择一次 **OM 输出合并**操作，从 Vulkan 应用一路追到设备上的 per-fragment 数学，画出完整的调用与依赖图，并标注「哪一段是 FF 快路径、哪一段是软件回退、哪一段是主机预言机」。

**建议步骤**：

1. 从设计文档的栈图出发（[docs/designs/graphics_software_stack.md:L138-L190](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md#L138-L190)），标出 `FS ──► OM` 这一段。
2. **FF 快路径分支**：FS 用 `vx_om4` 内联函数（见 u10-l1），到达硬件 OM 单元。在 SimX 里，这就是 [sim/simx/om/om_core.cpp:L137-L141](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/om/om_core.cpp#L137-L141) 的 `DepthTencil`/`Blender`，它们 [转发到 gfx_sw::](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_ff_model.cpp#L91-L110)。
3. **软件回退分支**：若该 OM 状态 FF 表达不了（如 `R8` 颜色格式），FS 改调 `gfx_om_fragment_sw`（[gfx_sw_abi.cpp:L53-L60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/gfx/gfx_sw_abi.cpp#L53-L60)），它 `reinterpret_cast` 后调 [gfx_sw::om_fragment](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/gfx_sw.h#L597-L602) → `om_sample_rmw`。
4. **主机预言机分支**：主机侧 MSAA 一致性测试（[tests/unittest/gfx_msaa/main.cpp:L1-L25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/unittest/gfx_msaa/main.cpp#L1-L25)）直接 include `gfx_sw.h` 调 `om_fragment_msaa`，对独立 oracle 校验。
5. 在图上用同一种颜色标出「`gfx_sw::om_sample_rmw` / `ds_test` / `blend`」——你会看到**三条分支汇聚到同一个数学函数**，这就是「单一真相来源」的视觉证据。
6. 最后写一段话解释：为什么这种设计让 SimX↔RTL 的 model_parity（见 u7-l4）天然成立——因为主机 FF 模型、设备 FF 单元的 SimX 镜像、设备软件回退，三者跑的都是同一份 `gfx_sw::` 数学。

**验收标准**：你的图里应当有三个「调用方」（SimX OM 单元、设备 FS 软件回退、主机一致性测试），它们最终都指向 `sw/common/gfx_sw.h` 里的同一组合并函数；且你能解释 `libgfx_sw.mk` 的 divergence 标志为何只约束设备构建、不约束主机构建。

## 6. 本讲小结

- Vortex 图形栈是「双轨」：FF 硬件快路径 + 设备侧 SIMT 软件回退；北极星是 true GPU——submit 到 present 全程设备常驻、绝不走主机回路。
- **主机侧 FF 软件模型** `gfx_ff_model.{h,cpp}` 模拟 RASTER/TEX/OM，被 SimX 消费（`om_core`/`raster_core`/`tex_core` 都 include 它），让图形测试在没有真实 FF 硬件时也能跑——这就是「软件发射器」的字面含义。
- 工程核心是 **单一真相来源**：每像素的深度/模板测试、混合、逻辑操作、纹理采样、光栅覆盖数学只写一份（`gfx_sw.h`/`gfx_frag_tex.h`/`gfx_frag_rast.h`），主机 FF 模型与设备软件回退编译同一份代码，故「逐位一致因为就是同一份代码」。
- **C ABI 桥** `gfx_sw_abi.{h,cpp}` 让 Mesa 的 C 驱动能调用 C++ 数学：靠 POD 描述符 + `static_assert` 守布局逐字节一致 + `reinterpret_cast` 转发；`covered` 参数把发散分支收进 ABI 单元以保持 FS 直线程。
- 设备软件回退的 OM 合并会撑爆 Vortex divergence pass 的 100-BB 上限，故 `libgfx_sw.mk` 用 `-mllvm -vortex-divergence-max-bbs=512` + `GFX_SW_DIVERGENCE_OK` 守住，`gfx_sw.h` 用 `#error` 强制设备构建必须带上该标志（主机构建无此 pass，故豁免）。
- `sw/gfx` 还住着设备图形前端内核（`expand_k`/`setup_k`/`binning_k`），是「设备常驻前端」的实体；注意区分本讲的「FF 行为软件模型/回退」与 `sw/runtime` 里 `program_raster/om/tex` 那种「FF 寄存器打包器」。

## 7. 下一步学习建议

- **横向打通 FF 硬件实现**：回到 [u10-l1（图形硬件栈）](u10-l1-graphics-hw.md)，对照确认 RASTER/TEX/OM 的 RTL 单元与本讲的 `gfx_ff_model` 类一一对应——这就是 graphics_parity / model_parity 的物理基础。
- **纵向打通驱动侧**：阅读 `docs/designs/vortexpipe_architecture.md`（u12-l2 会用到），看 vortexpipe 如何把一个 Vulkan `vkCmdDraw` 经 NIR→LLVM 编译、最终经 `DrawCommands` 与 `FrontEndPool`（[sw/runtime/include/graphics.h:L169-L207](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/include/graphics.h#L169-L207)）落到本讲的设备前端内核与 FF/软件回退上。
- **动手验证一致性**：跑 `tests/unittest/gfx_msaa` 与 `tests/unittest/gfx_rast_sw` 两个主机单元测试，它们用「设备跑的同份代码」对独立 oracle 校验，是「单一真相」最直接的体感。
- **进阶阅读源码**：精读 `sw/common/gfx_frag_tex.h` 与 `sw/common/gfx_frag_rast.h` 两个共享头——前者是纹理采样数学、后者是光栅覆盖行走（含 Vulkan top-left fill rule），它们是本讲反复提到的「共享数学」的另一条腿。
