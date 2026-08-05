# Vulkan 支持（vortexpipe / mesa）

## 1. 本讲目标

本讲讲解 Vulkan 是如何跑在 Vortex 上的。读完本讲，你应该能够：

1. 说出 Vortex 图形栈的「两棵树」分工——`mesa_vortex`（驱动）与本仓库（平台/SDK），以及它们之间单向的 SDK 边界。
2. 理解 `vortexpipe` 是 Mesa `llvmpipe` 之上的「薄装饰器」，并解释「继承并加速」(inherit and accelerate) 的回退契约。
3. 描述 Vulkan 的 `vkCmdDraw` 如何被翻译成 NIR、再经 LLVM-IR 编译成设备侧 `.vxbin`，最终落到 RASTER/TEX/OM 固定功能单元与片段着色器。
4. 在 `tests/vulkan` 里定位并运行一个 Vulkan 测试，读懂它对 `vortexpipe` 驱动与 `VORTEX_DRIVER` 后端的调用路径。

## 2. 前置知识

在继续前，请确认你已掌握 u12-l1（OpenCL/PoCL）建立的几条认知，本讲会直接复用它们：

- **ICD（Installable Client Driver）机制**：上层 API（OpenCL 用 `libOpenCL.so`，Vulkan 用 Vulkan loader）通过一份 JSON「名片」发现并加载底层驱动。
- **`libvortex.so` 的 stub 分发**：所有上层栈最终都汇聚到 `vortex2.h` / `vortex.h` 运行时 API，再由 stub 按 `$VORTEX_DRIVER` 在 simx/rtlsim/opae/xrt 后端间 `dlopen` 切换（见 u3-l3）。
- **`.vxbin` + KMU 启动模型**：设备内核被编译/打包成 `.vxbin`，主机通过命令处理器（CP）→ KMU 发射 CTA（见 u3-l4、u4-l1）。

本讲与 u12-l1 的关键差异：OpenCL/PoCL 是**纯计算**路径（kernel 直接映射到 SIMT 核）；而 Vulkan/lavapipe/vortexpipe 是**完整图形**路径——它不仅要调度 SIMT 核，还要驱动 RASTER（光栅化）、TEX（纹理）、OM（输出合并）三个固定功能单元（FF，Fixed-Function）。本讲重点就是这条图形专属链路。

补充几个 Mesa 术语（初学者可能不熟）：

- **Mesa**：Linux 上开源的图形驱动框架，它把 OpenGL/Vulkan 等 API 统一抽象成一套内部接口。
- **Gallium**：Mesa 的驱动模型，定义了 `pipe_screen`（设备能力）、`pipe_context`（绘图/调度上下文）等标准接口，驱动只需实现这些接口。
- **lavapipe**：Mesa 的「CPU 版 Vulkan」前端。它把 Vulkan 翻译成 Gallium 调用，再用 CPU（`llvmpipe`）软渲染。Vortex 复用了它的 Vulkan 前端。
- **llvmpipe**：Mesa 的 CPU 软件光栅器，基于 LLVM。
- **NIR**：Mesa 的中间表示（Near-IR），所有 shader 最终都降级到 NIR 再交给各驱动。

## 3. 本讲源码地图

本讲横跨「两棵树」，但本仓库只包含其中一棵。下表列出关键文件：

| 文件（本仓库） | 作用 |
|----------------|------|
| [docs/designs/graphics_software_stack.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md) | **总地图**：说明图形相关源码在两棵树里各住哪里、如何从 Vulkan 应用层层下落到硬件。 |
| [docs/designs/vortexpipe_architecture.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md) | **驱动详解**：vortexpipe 的软件架构、编译架构、渲染管线。 |
| [tests/vulkan/common.mk](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/common.mk) | Vulkan 测试套件的共享构建规则，揭示如何用 lavapipe ICD + `GALLIUM_DRIVER=vortexpipe` 驱动 Vortex。 |
| [tests/vulkan/triangle/main.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/triangle/main.c) | 最小 Vulkan「画三角形」测试，是本讲的实践样例。 |
| [sw/kernel/include/vx_graphics.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_graphics.h) | 设备侧 TEX/OM/RASTER 内联函数（图形 ISA 的真实编码锚点）。 |
| [sw/kernel/include/vx_gfx_window.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_gfx_window.h) | 图形寄存器窗口（SETW/GETW）原语，FF 单元经它暂存操作数。 |
| [README.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md) | 顶层说明：把 Vulkan 列为支持的软件栈，并把 mesa 列为工具链依赖。 |

另一棵树 `mesa_vortex`（分支 `prism`，仓库 `github.com/vortexgpgpu/mesa`）里的 `src/gallium/drivers/vortexpipe/` 才是 vortexpipe 驱动本体（`vp_context.c`、`vp_compile.c`、`vp_nir_to_llvm.c`、`vp_raster.cpp` 等）。这些文件**不在本仓库**，本讲通过上述设计文档来描述它们，不为本仓库之外的文件伪造永久链接。

## 4. 核心概念与源码讲解

### 4.1 两棵树与 SDK 边界

#### 4.1.1 概念说明

Vortex 的图形栈刻意拆成两个独立仓库，分工清晰：

- **`mesa_vortex`（驱动树）**：Vortex 的 Vulkan/Gallium 驱动，即 `vortexpipe`。它由 Mesa 的 lavapipe 前端驱动。
- **本仓库（平台树）**：Vortex 平台本体——SDK（运行时 + 设备内核 + ABI 头）、SimX 模型、RTL。它是「图形 + 光追」的**唯一真相来源**。

为什么要拆？因为这与真实 GPU 厂商的做法一致：用户态驱动（userspace driver）消费一份 GPU SDK。mesa 把本仓库当作 SDK 来用——读取 `$VORTEX_PATH`（安装目录）拿头文件和 `libvortex.so`，读取 `$VORTEX_HOME`（源码目录）拿设备内核源码和工具链。这是一条**单向**依赖：`mesa → Vortex`，方向不可逆。

#### 4.1.2 核心流程

```
  Vulkan 应用
      │  vkCmdDraw / vkCmdDispatch
      ▼
┌─ mesa_vortex ────────────────────────┐
│  lavapipe（Vulkan→Gallium 翻译）       │
│      │  pipe_screen / pipe_context    │
│      ▼                                │
│  vortexpipe（Gallium 驱动，拦截 vtable）│
└──────────┬───────────────────────────┘
           │  vortex2 API（libvortex.so）+ 线上 ABI
   ════════╪═══ SDK 边界（$VORTEX_PATH / $VORTEX_HOME）════
           ▼
┌─ 本仓库（Vortex 平台）─────────────────┐
│  sw/runtime  libvortex.so（vortex2 API）│
│  sw/gfx      设备内核（前端 + 软件回退） │
│  sw/common   ABI 契约（vx_gfx_abi.h 等） │
│      │                                 │
│      ▼   后端三选一（运行时由 VORTEX_DRIVER 选）▼
│  sim/simx     hw/rtl       XRT/FPGA    │
└────────────────────────────────────────┘
```

这条路径的「北极星」目标是 **gfx_v2「真 GPU」**（design 文档称之为 the north star，见 [docs/designs/graphics_software_stack.md:31-42](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md#L31-L42)）：从 submit 到 present 的整条流水线**全程设备常驻、主机不介入**。主机只负责编译 shader、构建命令/状态块；设备侧前端（顶点装配→三角形 setup→bin 排序）和 FF 单元（RASTER 推片段→FS 跑 `vx_tex4`/`vx_om4`）在常驻内存里完成整次绘制。主机的 `Binning()` 参考渲染器只作为**离线 oracle**，不是运行时路径。

#### 4.1.3 源码精读

[docs/designs/graphics_software_stack.md:20-29](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md#L20-L29) 明确定义了两棵树与单向依赖：mesa_vortex 提供 vortexpipe 驱动，本仓库提供平台；驱动以 SDK 形式消费平台。

[docs/designs/graphics_software_stack.md:201-210](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md#L201-L210) 定义两个边界：

- **SDK 边界**：mesa 经 `$VORTEX_PATH`（头 + `libvortex.so`）与 `$VORTEX_HOME`（`sw/gfx` 内核源 + 工具链）消费 SDK，单向；线上 ABI（`vx_gfx_abi.h`）是硬件契约，归 SDK 所有。
- **后端边界**：同一份 SDK + 内核可跑在 SimX、RTL 或 FPGA 上，运行时由 `VORTEX_DRIVER` 选择。

[README.md:113-120](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L113-L120) 说明下游工具（mesa-vortex、pocl-vortex、chipstar）**只**通过 `$VORTEX_PATH` 和 pkg-config 集成 Vortex——这与 CUDA/ROCm/oneAPI SDK 的形态完全一致。`make install` 在 `$VORTEX_PATH` 下铺出公共头、库和 pkg-config 文件。

[docs/designs/graphics_software_stack.md:212-220](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md#L212-L220) 阐述「单一真相来源」原则：设备内核只在 `sw/gfx/`（前端 + 软件回退）和 `sw/kernel/include/`（内联函数）各存一份，SimX 图形测试和 vortexpipe 编译**同一批文件**——没有重复、不会漂移。

#### 4.1.4 代码实践

**目标**：确认两棵树的依赖方向与边界位置。

**步骤**：

1. 在本仓库执行 `make install` 后，列出 `$VORTEX_PATH` 下的 `include`、`lib` 目录，确认 `vortex2.h` 与 `libvortex.so` 存在。
2. 查阅 `Makefile.in`（u1-l3 介绍过），找到 `$VORTEX_PATH/kernel/` 与 `$VORTEX_PATH/runtime/` 的安装布局。
3. 在 `docs/designs/graphics_software_stack.md` 的「The stack」图里，圈出「SDK boundary」这一行。

**需要观察的现象**：`$VORTEX_PATH` 里只有 SDK（头 + 库 + pkg-config），没有任何 mesa 驱动源码——驱动源码在 `mesa_vortex` 那一边。

**预期结果**：确认 mesa → Vortex 是单向依赖，本仓库不需要知道 mesa 的存在。

**待本地验证**：若你尚未 `make install`，可只阅读 [README.md:113-120](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L113-L120) 完成步骤 3。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vx_gfx_abi.h`（FF 单元的线上 ABI）归 SDK 所有，而不是归 mesa 驱动？

**答案**：因为它既是主机侧运行时的契约，也是设备侧硬件的契约——SimX 图形模型 `#include` 它当 oracle，vortexpipe 也依赖它打包缓冲。把它放在 `sw/common`（SDK 一部分）才能让「同一份 ABI」跨主机/设备/SimX/驱动复用，符合单一真相来源。

**练习 2**：「后端边界」与「SDK 边界」是同一个东西吗？

**答案**：不是。SDK 边界分隔 mesa 驱动 与 Vortex 平台（源码层）；后端边界分隔 Vortex 平台 与 simx/rtl/fpga 三种实现（运行时层）。前者是编译期单向依赖，后者是运行时 `$VORTEX_DRIVER` 选择。

---

### 4.2 vortexpipe 软件架构：llvmpipe 上的薄装饰器

#### 4.2.1 概念说明

`vortexpipe` 不是从零写的驱动，而是 llvmpipe 之上的**薄装饰器**（thin decorator）。它做了三件事：

1. **接管** llvmpipe 的 `pipe_screen` / `pipe_context` 生命周期，以便把 vortexpipe 自己的状态穿插进去。
2. **只覆盖**它特化的入口点：上下文创建、compute 钩子（`launch_grid` 等）、图形管线状态 + draw 钩子（VS/FS/深度模板/blend/顶点元素/纹理采样/帧缓冲/`draw_vbo`）。
3. **其余**全部原样转发给 llvmpipe。

这样做的好处：避免写一个约 140 个 thunk 的「全装饰器」，覆盖面小到一眼能审计。

#### 4.2.2 核心流程

```
              Vulkan app
                  │
            ┌─────▼──────┐
            │  lavapipe  │   （Vulkan → Gallium 翻译）
            └─────┬──────┘
                  │ pipe_screen / pipe_context
            ┌─────▼──────┐
            │ vortexpipe │   ← 本驱动：vtable 拦截
            └─────┬──────┘
                  │ 转发的 vtable 调用
            ┌─────▼──────┐
            │  llvmpipe  │   （CPU 基线 + util_blitter）
            └────────────┘
```

Vortex 设备**位于**这套 CPU 栈之**旁**（beside），通过 `libvortex.so`（头 `vortex2.h`）访问；vortexpipe 的特化入口是唯一会触碰 `vx_*` 调用的地方。

「回退契约」是这里的关键设计：vortexpipe 是「llvmpipe 之上的尽力而为加速器」——每个被覆盖的入口要么在 Vortex 上成功，要么**转发给保存的 llvmpipe 槽位**。回退是刻意的，让驱动即使遇到尚未覆盖的管线状态组合，也能暴露完整 Gallium 能力（并通过 lavapipe 自测）。回退是**逐次调用**判定，而非逐管线——相邻两次 draw 可以一个跑 Vortex、一个回退到 llvmpipe。

CI 等场景下「静默回退」会掩盖回归，因此有 **STRICT 模式**（由 `$MESA_VORTEX_STRICT` 门控）：开启后，缺失的 Vortex 路径会变成 `mesa_loge` 报错并把调用变 no-op，让应用自己的校验步骤发现数据没落地。

#### 4.2.3 源码精读

[docs/designs/vortexpipe_architecture.md:27-48](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L27-L48) 解释「薄装饰器」策略：就地 patch vtable 槽位、把原指针存进 side struct（`vp_screen`/`vp_context`，用进程级哈希表 `vp_reg_put/get/del` 索引）。之所以 patch vtable 合法，是因为 vortexpipe **自己创建了** llvmpipe screen，它拥有这个基对象。

[docs/designs/vortexpipe_architecture.md:55-75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L55-L75) 给出分层图：Vulkan app → lavapipe → vortexpipe（vtable 拦截）→ llvmpipe（转发）；Vortex 设备经 SDK 在栈之旁。

[docs/designs/vortexpipe_architecture.md:134-159](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L134-L159) 定义回退契约与 STRICT 模式：覆盖入口要么成功要么转发；逐次调用判定；`$MESA_VORTEX_STRICT` 把静默回退变成报错 no-op。

回到本仓库的真实代码——STRICT 模式在测试侧被强制开启。[tests/vulkan/common.mk:77-87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/common.mk#L77-L87) 默认 `STRICT ?= 1`，注释说明：严格模式关闭时，缺失内核/运行时失败/NIR 翻译缺口只是 `logw` + CPU 回退，测试仍会 PASS（因为 llvmpipe 算对了）；预期跑在 Vortex 上的测试必须开 STRICT，harness 才会在任何回退时失败。

[tests/vulkan/common.mk:110-123](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/common.mk#L110-L123) 的 `check_run` 门控是 STRICT 在工程上的兜底：它把测试输出里任何 `MESA: error` 行判为失败（捕获 vortexpipe 里每条 `mesa_loge`：工具链失败、运行时 API 失败、STRICT 回退拒绝），并要求测试打印的设备名含 `vortex`（防止 lavapipe 静默选了 llvmpipe——llvmpipe 报告的名字是 `llvmpipe (LLVM …)`）。

#### 4.2.4 代码实践

**目标**：理解 STRICT 模式如何把「静默回退」变成可检测的失败。

**步骤**：

1. 阅读 [tests/vulkan/common.mk:77-87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/common.mk#L77-L87)，记下 `STRICT` 默认值与它改变的行为。
2. 阅读 `check_run`（[common.mk:110-123](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/common.mk#L110-L123)）的三条判据（退出码、`MESA: error`、设备名含 `vortex`）。
3. 设想：若把某个测试的 `STRICT := 0`，它的 fragment shader 又恰好用了 TCU 不支持的纹理模式，会发生什么？

**需要观察的现象**：严格模式下驱动会 `mesa_loge` 并拒绝，`check_run` 捕获到 `MESA: error` → 测试失败；非严格模式下驱动静默回退到 llvmpipe，结果「正确」但没跑在 Vortex 上，`check_run` 会因设备名不含 `vortex` 而失败。

**预期结果**：理解 STRICT + `check_run` 是双重防线，前者让驱动自己报错，后者从测试输出兜底。

**待本地验证**：实际运行需先安装 mesa-vortex 工具链（见 §5）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 vortexpipe 能合法地「就地 patch」llvmpipe 的 vtable？

**答案**：因为它自己创建了 llvmpipe 的 `pipe_screen`，是基对象的所有者；patch 一个自己拥有的对象是合法的，否则就是篡改别人的 vtable。

**练习 2**：回退是「逐管线」还是「逐次调用」？为什么这个区别重要？

**答案**：逐次调用。重要之处在于：一次 draw 的 VS 若可翻译会在 Vortex 上跑，但它的 FS 若用了未覆盖的特性，VS 仍跑在 Vortex、光栅化则在 llvmpipe 上用缓存的 passthrough VS 继续——而不是整管线一起回退。

---

### 4.3 编译架构：NIR → LLVM-IR → .vxbin 与图形 ISA 选择

#### 4.3.1 概念说明

Vortex 把 Vulkan shader 翻译成设备可执行 `.vxbin` 的编译流水线是 **Shape C**：标量 walker 把 NIR 逐条走一遍，发射成单个 LLVM-IR 模块，再交给 Vortex 设备工具链编译链接成 `.vxbin`。之所以选 Shape C，是因为另外两种形状被否决了：fork llvmpipe 的 SoA codegen（约 238KB，太大难维护）、SPIR-V 往返（`llvm-spirv` 拒绝 Vulkan 风味的 SPIR-V）。

关键点：**没有逐指令的「该不该用 TEX」决策**。图形 ISA 的选择发生在更早、更清晰的三个层次——按 shader stage、按 NIR opcode、按设备能力做 HW/SW 路由。

#### 4.3.2 核心流程

```
   NIR shader（来自 lavapipe，已做 SPIR-V→NIR→opt+lowering）
        │
        ▼
   vp_nir_to_llvm        ← 标量 walker，发射一个 LLVM-IR 模块
        │
        ▼
   LLVM IR 文本（riscv32/64-unknown-elf +xvortex +zicond）
        │
        ▼
   vp_compile_vxbin
        ├──→ system("clang … -lvortex2 …")   ← 链接 libvortex2.a（KMU 设备运行时）
        ▼
   .vxbin（内核镜像）
        │
        ▼
   vp_launch / vp_launch_vs / vp_raster_draw
        └──→ vx_module_load_file + vx_enqueue_launch
```

三种 shader stage 映射到两种输出形状：

| NIR stage | LLVM 函数形状 | KMU 入口 |
|-----------|---------------|----------|
| compute   | `void kernel_main(ptr %arg)` — 每个 work-item 一线程 | `kernel_main` |
| vertex    | `void kernel_main(ptr %arg)` — 每个顶点一线程 | `kernel_main` |
| fragment  | `void fs_main(...)` 被一条直线型 run-once 的 `kernel_main` 包裹（`emit_fs_wrapper`） | wrapper 的 `kernel_main` |

编译后端 `vp_compile_vxbin` 通过 **fork/exec 现有的 Vortex 设备工具链**把 LLVM-IR 文本变成 `.vxbin`，关键标志镜像了本仓库 `tests/regression/common.mk` 的规范调用：`--target=riscv{32,64}-unknown-elf`、`-Xclang -target-feature -Xclang +xvortex`（正是它让 Clang 的 RISC-V 后端发射 Vortex 内联函数 + SIMT 分支发散 pass）、`+zicond`、`-mllvm -disable-loop-idiom-all`。链接行拉入 `libvortex2.a`（KMU 设备内核运行时，提供 `vx_start.S`、`vx_putchar`、`vx_spawn2.h` 的 `__syncthreads` 等），结果 ELF 再由 `sw/kernel/scripts/vxbin.py` 打包成 `.vxbin`——与 u12-l1 的 PoCL、本仓库原生内核共用**同一镜像格式**。

#### 4.3.3 源码精读

[docs/designs/vortexpipe_architecture.md:186-207](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L186-L207) 给出编译流水线全图：NIR → `vp_nir_to_llvm` → LLVM-IR 文本 → `vp_compile_vxbin`（fork clang + 链接 `libvortex2.a`）→ `.vxbin` → `vp_launch` 的 `vx_module_load_file + vx_enqueue_launch`。

[docs/designs/vortexpipe_architecture.md:371-399](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L371-L399) 详解后端标志：`+xvortex` 让后端发射 Vortex 内联函数与 SIMT 分支发散 pass；`+zicond` 用于发散控制流折叠；链接 `libvortex2.a`；XLEN 由 `$MESA_VORTEX_XLEN` 选（默认 32）。这个 env 名是 mesa 命名空间，避免与被链接的 `libvortex.so` 运行时读取的变量冲突。

**图形 ISA 编码（本仓库真实锚点）**。Vortex 图形 ISA 用 RISC-V **custom-1 opcode**（十进制 43 = 0x2B）。[sw/kernel/include/vx_graphics.h:23-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_graphics.h#L23-L32) 把编码写在头注释里：`funct3=2` 是 `vx_om4`（输出合并，windowed），`funct3=5` 是 `vx_tex4`（纹理采样，windowed）；RASTER 在 v2 里**没有内核 op**——光栅引擎自己在设备上启动片段着色器（push 模型）。

`vx_om4` 的真实内联汇编见 [sw/kernel/include/vx_graphics.h:81-84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_graphics.h#L81-L84)：`.insn r RISCV_CUSTOM1, 2, 0, x0, ...` 即 custom-1 funct3=2、`rd=x0` 的 fire-and-forget 提交。

[docs/designs/vortexpipe_architecture.md:346-360](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L346-L360) 给出完整 funct3 表（与 SDK `vx_graphics.h` 逐字节一致，并对照 `hw/rtl/core/VX_decode.sv` + `sim/simx/decode.cpp` 验证）：

| `funct3` | 助记符 | 作用 |
|----------|--------|------|
| 2 | `vx_om4` | 提交 2×2 quad 给 OM（R 型，`rd=x0`） |
| 4 | `GETWS` | 按槽读窗口（FS 读 frag 记录，按 `block_idx`） |
| 5 | `vx_tex4` | TEX 采样，单/quad 由 `funct7.mode` 区分 |
| 6 | window | `SETW`/`GETW`/`GETWF`/`CB_RET`（由 `funct2` 区分） |
| 7 | RTU | `TRACE2`/`WAIT2`（由 `funct2` 区分） |

`funct3=1/3` 未分配，解码器会 abort；旧形式 `vx_tex`(1)、3 操作数 `vx_om`(2)、`vx_rast`(3)、`vx_rast_begin`(4) 已在 sw+simx+rtl+mesa 全部退役。`vx_barrier` 在 custom-0（opcode 11），因为 custom-1 专留给图形 + RTU。

**HW/SW 路由**。[docs/designs/vortexpipe_architecture.md:315-336](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L315-L336) 说明：FS 编译期，运行时**逐 FF 单元**决定每个阶段跑在硬件单元还是设备侧 SIMT 软件回退（`libgfx_sw`）。`vp_fs_routing` 从 `has_raster/has_om/has_tex`（缓存的 `VX_ISA_EXT_*` 位）算出 `sw_tex/sw_om/sw_raster`。一个单元缺失或不胜任只把**该单元**路由到软件，而非把整次 draw 回退到 llvmpipe。

#### 4.3.4 代码实践

**目标**：把图形 ISA 编码与设备侧真实内联函数对应起来。

**步骤**：

1. 打开 [sw/kernel/include/vx_graphics.h:23-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_graphics.h#L23-L32)，找到 `vx_om4` 的 funct3 值（2）。
2. 读 [sw/kernel/include/vx_graphics.h:81-84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_graphics.h#L81-L84) 的 `.insn r %0, 2, 0, x0, %1, %2`，逐字段对照：opcode=`RISCV_CUSTOM1`、funct3=`2`、`rd=x0`。
3. 对照 funct3 表（[vortexpipe_architecture.md:346-360](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L346-L360)）确认 `vx_tex4` 是 funct3=5、window 是 funct3=6、RTU 是 funct3=7。

**需要观察的现象**：OM 与 TEX 都是 windowed（操作数经 SETW 暂存到共享图形寄存器窗口），且都用 custom-1 opcode 靠 funct3 区分。

**预期结果**：你能在头文件里用 funct3 把每条图形指令对上号，理解「custom-1 是图形 + RTU 专用 opcode 槽」。

**待本地验证**：纯源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么编译后端要 fork/exec 外部 clang，而不是用进程内 LLVM API？

**答案**：为了把前端（NIR→LLVM-IR 翻译器）与设备侧工具链解耦——进程内 LLVM API 更干净但被推迟；shell 出去能复用本仓库规范的 Vortex 工具链调用，降低耦合。

**练习 2**：`+xvortex` 这个 target-feature 在编译中起什么决定性作用？

**答案**：它让 Clang 的 RISC-V 后端发射 Vortex 专属内联函数与 SIMT 分支发散 pass（把发散控制流降级为掩码执行）；显式禁用（`-mllvm -vortex-branch-divergence=0`）会破坏内核依赖的 SIMT 语义。

---

### 4.4 渲染管线：一次 vkCmdDraw 端到端

#### 4.4.1 概念说明

这是本讲的核心——一次 Vulkan `vkCmdDraw` 在设备常驻路径上到底做了什么。要点是：硬件光栅路径下，**整次 draw 是一笔设备常驻事务**。VS 被折进前端的 stage 0（不回读变换后的顶点），设备侧 sort-middle 前端产出 RASTER 缓冲；运行时路径里**没有主机的 `graphics::Binning`**（它只作覆盖率的 oracle）。整次 draw 被记录成一个 `DrawCommands` 批，用一次 `vx_enqueue_draw` 提交（一次 doorbell、一次完成）。

#### 4.4.2 核心流程

整条绘制时间线（主机与设备分两栏）：

```
   主机                                │     设备
                                       │
   vp_draw_vbo                         │
     ├─ 资格检查（单 draw / 非实例化…）  │
     ├─（索引化）上传 index buf          │
     │                                  │
     └─ vp_raster_draw                  │   ── 一笔 vx_enqueue_draw 批（OP_DRAW）──
         ├─ 构建 DrawCommands 批         │
         ├─ 配置 RASTER/OM/TEX DCR       │ ─► FF 配置 + FS 启动描述符（FRAG_PC）
         ├─ vx_enqueue_draw  ────────►   │ ─► CP 在设备侧展开 draw：
         │                              │      expand_k  (VS 装配)   → setup_vertex_t
         │                              │      setup_k   (clip+cull) → rast_prim_t
         │                              │      binning_k (sort-middle)→ primbuf + 头
         │                              │      RASTER walker→earlyZ→packer→dispatch
         │                              │        └ 每个 wave 启动 1-warp frag CTA（纯 DCR）
         │                              │           FS wrapper（run-once）：
         │                              │             frag = vx_frag_load()        (GETWS)
         │                              │             重算边；插值
         │                              │             fs_main(in,out,texstate)     (vx_tex4 | sw)
         │                              │             vx_om4(pos_mask|face, base)  (OM | sw)
         │                              │                       └► OM AXI master 写 cbuf/zbuf
         └─ vx_queue_finish             │   color/depth 常驻（present 是唯一出口）
```

前端三步：`expand_k`（VS 装配，每顶点一线程，写 `setup_vertex_t`）→ `setup_k`（近面裁剪 + 背面剔除 + 定点平面方程 setup，产出 120B 的 `rast_prim_t`）→ `binning_k`（精确容量的并行 sort-middle：count→scan→emit）。颜色/深度/纹理在 render pass 里常驻（pinned-PA），由前端在设备侧绑定，不走主机往返。

随后 `vp_raster_draw` 配置 RASTER/OM/TEX 的 DCR，并让 RASTER 引擎**自己启动**片段着色器：它写 RASTER 的 fragment-shader 启动描述符（`VX_DCR_RASTER_FRAG_PC_LO/HI`、`FRAG_ENTRY`、`FRAG_PARAM`），光栅引擎的固定功能 walker→early-Z→packer→dispatch 在 core-local KMU 上**为每个覆盖 quad wave 启动一个裸 1-warp fragment CTA**（纯 DCR，没有主机的 FS grid launch）。

**FWD-5 push 的片段着色器（run-once）**：发射的 `kernel_main` wrapper 每个 wave 跑一次：

```
frag         = vx_frag_load()                       // GETWS，slot = block_idx
prim         = arg[0] + frag.pid * 120
(qx,qy,mask) = decode(frag.pos_mask)
for 每个覆盖子像素 i:
    (f0,f1,f2) = 重算边值  a·X + b·Y + c  at pixel (X,Y)
    dx = f0/(f0+f1+f2);  dy = f1/(f0+f1+f2)
    interpolate(prim.rast_attribs, dx, dy) → fs_in
    fs_main(fs_in, fs_out, texstate)                // vx_tex4  | gfx_tex_sample_sw
    rgba  = pack(fs_out)
    depth = fixed24(plane_z(prim, X, Y))
    vx_om4(frag.pos_mask | face<<31, om_slot_base)  // vx_om4   | gfx_om_fragment_sw
```

这里没有 `vx_rast`/`vx_om` 的拉取，也没有 bcoord CSR 读取——载荷在启动时已 seed 到窗口，边值从图元重算。`vx_om4` 把覆盖 quad 提交给 OM 单元，后者做深度测试/blend/写颜色+深度；FS 永远看不到附件地址。

插值用到的是重心坐标。对一个三角形，三个顶点的边方程平面值 \(f_0,f_1,f_2\) 在像素 \((X,Y)\) 处为：

\[
f_k = a_k \cdot X + b_k \cdot Y + c_k,\quad k=0,1,2
\]

像素的重心坐标（归一化后）为：

\[
(\lambda_0,\lambda_1,\lambda_2) = \frac{1}{f_0+f_1+f_2}(f_0,\ f_1,\ f_2)
\]

属性则按这些重心权重做透视正确插值（透视修正已在 `setup_k` 用 \(1/w\) 预乘完成）。

#### 4.4.3 源码精读

[docs/designs/vortexpipe_architecture.md:458-477](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L458-L477) 是 Stage 0 资格检查：vortexpipe 只对「简单直接或索引化、非实例化、单次 draw 且 VS 可翻译」的情况走 Vortex 路径，其余整体回退 llvmpipe（或 STRICT 下报错）。

[docs/designs/vortexpipe_architecture.md:478-508](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L478-L508) 描述 `vp_raster_draw` 的设备编排：VS 折进前端 stage 0，设备侧 sort-middle 前端产出 RASTER 缓冲，运行时无主机 `Binning`；整次 draw 是一笔 `DrawCommands` 批，含前端三段启动 + FF DCR 写，由 CP 的 launch-barrier 按序排空。

[docs/designs/vortexpipe_architecture.md:510-528](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L510-L528) 讲 FF 配置 + RASTER 启动：配 RASTER DCR（含 FS 启动描述符）、OM DCR、TEX DCR，然后 RASTER 自启动 FS（纯 DCR，无主机 FS grid launch）。

[docs/designs/vortexpipe_architecture.md:530-557](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L530-L557) 是 FWD-5 push、run-once 的片段着色器伪代码（即上面那段）。

设备侧真实锚点：[sw/kernel/include/vx_graphics.h:86-95](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_graphics.h#L86-L95) 注释明确：RASTER dispatch v2 是 **PUSH**——光栅引擎的 work distributor 每个 wave 启动一次片段着色器（无 pull op）；每 lane 载荷在 warp 启动时已 seed 到 gfx 寄存器窗口（零 LMEM/LSU 流量）。`vx_frag_load` 宏见 [sw/kernel/include/vx_graphics.h:114-118](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_graphics.h#L114-L118)：读 `{pos_mask, pid}`，没有 bcoord 载荷。

[docs/designs/graphics_software_stack.md:180-197](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_software_stack.md#L180-L197) 给出设备常驻 render flow：CP→expand_k→setup_k→binning_k→RASTER（push）→FS→TEX→OM→framebuffer；任何 FF 无法表示的状态由设备侧 SIMT 软件回退覆盖，绝不走主机往返。

#### 4.4.4 代码实践

**目标**：跟踪一次 `vkCmdDraw` 从主机到设备 RASTER/FS/OM 的完整路径。

**步骤**：

1. 打开 [tests/vulkan/triangle/main.c:319](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/triangle/main.c#L319) 的 `vkCmdDraw(cmd, 3, 1, 0, 0)`——这是唯一的绘制调用（3 个顶点的三角形列表）。
2. 顺着设计文档反推它经过的层：`vkCmdDraw` → lavapipe → `vp_draw_vbo` 资格检查 → `vp_raster_draw` 构建 `DrawCommands` 批 → `vx_enqueue_draw` → CP 设备侧 expand/setup/binning → RASTER 启动 FS。
3. 在 [sw/kernel/include/vx_graphics.h:86-95](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_graphics.h#L86-L95) 确认 FS 是被 RASTER push 启动的，不是 shader 自己 pull。

**需要观察的现象**：`triangle/main.c` 里没有任何 `vx_*` 调用——纯标准 Vulkan。`vx_*` 全部藏在 vortexpipe 内部，应用对此无感。

**预期结果**：你能在脑中画出「Vulkan API → mesa-vortex → Vortex 图形/计算栈」的映射，标注出 RASTER/TEX/OM 三个 FF 单元与 push 式 FS。

**待本地验证**：实际跑通见 §5 综合实践。

#### 4.4.5 小练习与答案

**练习 1**：为什么 RASTER 路径下「整次 draw 是一笔设备常驻事务」？关键在于 VS 怎么处理？

**答案**：VS 被折进前端的 stage 0（`expand_k`），变换后的顶点不回读主机；sort-middle 前端在设备常驻内存里产出 RASTER 缓冲。所以从 submit 到 present 主机不介入绘制。

**练习 2**：FWD-5 push 模型下，FS 是怎么拿到自己覆盖像素信息的？

**答案**：RASTER 的 dispatch 在启动 FS warp 时，把每 lane 的载荷 seed 到该 warp 的图形寄存器窗口（`vx_frag_load` 读的 `{pos_mask, pid}`），FS 用 `block_idx` 索引读回——零 LMEM/LSU 流量，且没有 shader 发起的 `vx_rast` pull。

---

### 4.5 一致性模型：继承与加速 + 设备侧图形 ISA 锚点

#### 4.5.1 概念说明

vortexpipe 的 Vulkan 一致性模型叫 **inherit and accelerate（继承并加速）**：它继承 lavapipe 的完整 Vulkan 表面，把其中**一个子集**加速到 Vortex，未卸载的部分回退到 lavapipe 的 CPU 执行。因此 lavapipe 既是「未实现特性的回退」，又是**正确性 oracle**——任何 Vortex 加速的结果必须与 lavapipe 本会产生的结果一致。

但要小心一种陷阱：**静默坍缩（silent collapse）**。这不是回退，而是代码「接受调用、悄悄投射到最近的 gfx-v1 编码却不告诉调用方」，结果在合规输入上产生错误像素（而非拒绝绘制）。比如 mipmap/anisotropic 过滤悄悄坍缩成 POINT、clamp-to-border 坍缩成 CLAMP。这与「门控回退（gated fallback）」相对——后者会检测不支持的情况并路由到 llvmpipe。一致性纪律是：**门控回退永远优于静默坍缩**。

#### 4.5.2 核心流程

```
        Vulkan 调用
            │
   ┌────────┴────────┐
   │ 是否 gfx-v1 能表示？│
   └────────┬────────┘
      能    │          不能 / 缺失
   ┌───────▼───────┐   ┌─────────────────────┐
   │ Vortex 加速路径 │   │ 门控回退→llvmpipe    │ ← 正确（STRICT 下报错）
   │ （须与 oracle 等）│   │ 静默坍缩→错误像素     │ ← 已知一致性漏洞
   └───────────────┘   └─────────────────────┘
```

设计文档把当前实现的不变量总结为三条：(1) 图形固定功能硬件恰好是 RASTER、TEX、OM 三个单元，其余（顶点/片段/计算着色、binning 胶水）跑在 SIMT 核上；(2) R/T/O 数据通路是定点（gfx-v1），浮点工作在 SIMT 核上；(3) 驱动目标是 SimX 建模/可综合硬件，没有独立的「纯软件图形路径」，回退就是 llvmpipe CPU 执行。

#### 4.5.3 源码精读

[docs/designs/vortexpipe_architecture.md:848-859](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L848-L859) 定义一致性模型：继承 lavapipe 全 Vulkan 表面、加速子集、回退 CPU；lavapipe 既是回退又是正确性 oracle；实际承诺目标是 Vulkan 1.3 + 光追扩展族，而广告的表面仍是 lavapipe 的（当前 1.4）。静默坍缩审计（TEX/OM §3.6–§3.8）正是为防止「加速」悄悄变成「错误」。

[docs/designs/vortexpipe_architecture.md:159-178](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L159-L178) 区分两种选择：**门控**（检测不支持并路由到 llvmpipe，如 ISA-cap 缺失、非 `texop_tex` 的 NIR op、NPOT 纹理维度）vs **静默坍缩**（接受调用并投射到 gfx-v1 编码，如 mipmap→POINT、非 RGBA8 格式按 RGBA8 重解释）。

[docs/designs/vortexpipe_architecture.md:572-635](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L572-L635) 是 TEX 一致性漏洞的完整目录：哪些是门控（设备缺 `VX_ISA_EXT_TEX`、`nir_texop_txf`/`txs`/`lod` 被拒）、哪些是静默坍缩（mipmap/anisotropic、clamp-to-border、非 RGBA8），以及如何用 ~10 LOC 的门把它们逐个变成 gated fallback。

[docs/designs/vortexpipe_architecture.md:833-847](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L833-L847) 列出三条设计不变量（FF 恰为 RASTER/TEX/OM；定点 gfx-v1；驱动目标 SimX/RTL，无独立软件路径）。

设备侧图形窗口的真实布局见 [sw/kernel/include/vx_gfx_window.h:34-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_gfx_window.h#L34-L52)：32 槽共享寄存器窗口在图形与 RTU 间划分，frag 记录在槽 19–20（`VX_GFX_FRAG_SLOT_BASE`），与 RTU 用的槽互斥，使 FS 能带着 frag 记录跑完整条 ray query 而不被破坏。SETW/GETW 的编码见 [sw/kernel/include/vx_gfx_window.h:73-88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_gfx_window.h#L73-L88)：CUSTOM1 funct3=6，`funct2` 选 SETW(1)/GETW(3)，槽骑在 funct7[6:2]。

#### 4.5.4 代码实践

**目标**：学会从设计文档的一张一致性表里分辨「门控回退」与「静默坍缩」。

**步骤**：

1. 打开 TEX 漏洞目录 [vortexpipe_architecture.md:582-596](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L582-L596)。
2. 把每行的「Conformant?」列分类：标 **Yes — gated** 的是门控回退（正确），标 **No — silent collapse** 的是静默坍缩（错误像素）。
3. 阅读关闭静默坍缩的三步方案 [vortexpipe_architecture.md:604-635](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/vortexpipe_architecture.md#L604-L635)：sampler-state 门、texture-format 门、`emit_tex` 收紧。

**需要观察的现象**：所有静默坍缩项的修复都是「在不改变硬件编码的前提下，增加几行门控拒绝」——把「悄悄错」变成「拒绝并回退」。

**预期结果**：理解 gfx-v1 要通过 Vulkan-CTS，缺的不是硬件，而是把这些静默坍缩逐个改成门控回退。

**待本地验证**：纯源码阅读型实践。

#### 4.5.5 小练习与答案

**练习 1**：lavapipe 在 vortexpipe 里同时扮演哪两个角色？

**答案**：既是未实现特性的回退路径，又是正确性 oracle——任何 Vortex 加速的结果必须与 lavapipe 本会产生的逐像素一致。

**练习 2**：为什么「静默坍缩」比「门控回退」更危险？

**答案**：门控回退把不支持的情况路由到 llvmpipe，结果正确；静默坍缩则接受调用并悄悄投射到 gfx-v1 编码，在合规输入上产生**错误像素**而非拒绝绘制——CTS 会看到错的而非被拒的，难以发现。

---

## 5. 综合实践

**任务**：在 SimX 后端跑通一个 Vulkan 三角形测试，并把「Vulkan API → mesa-vortex → Vortex 图形/计算栈」的映射画成一张图。

**前置条件**：本实践需要 mesa-vortex 工具链。`tests/vulkan/common.mk` 假设 mesa 安装在 `$TOOLDIR/mesa-vortex`（由 `MESA_PATH` 指向），并要求 `glslc`（把 GLSL 编成 SPIR-V）。若环境未安装 mesa-vortex，请先阅读 [tests/vulkan/common.mk:17-25](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/common.mk#L17-L25) 与 [README.md:79-81](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L79-L81)（Mesa 列为工具链依赖），把本实践当作「源码阅读 + 待本地验证」。

**步骤**：

1. **读懂测试**：阅读 [tests/vulkan/triangle/main.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/triangle/main.c)。它在 64×64 RGBA8 离屏图像上画一个三角形，把结果拷到 host-visible 缓冲，再校验：中心像素在三角形内（非黑）、某角点是清除色（黑）、着色像素数在三角形预期覆盖范围内（[main.c:373-380](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/triangle/main.c#L373-L380)）。
2. **读懂运行环境**：阅读 [tests/vulkan/common.mk:92-98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/common.mk#L92-L98) 的 `RUN_ENV`，记下关键变量：
   - `VK_ICD_FILENAMES` 指向 lavapipe 的 ICD JSON；
   - `GALLIUM_DRIVER=vortexpipe` 选 Gallium 驱动；
   - `MESA_VORTEX_XLEN` / `MESA_VORTEX_STRICT` 控制编译位宽与严格模式；
   - `VORTEX_DRIVER=simx`（在 run-simx recipe 里，[common.mk:144-147](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/common.mk#L144-L147)）选后端。
3. **运行（若环境就绪）**：
   ```sh
   cd tests/vulkan/triangle
   make run-simx
   ```
4. **画图**：用本讲 §4.1.2 与 §4.4.2 的两张图作底，画一张完整映射图，至少标注：Vulkan app → lavapipe → vortexpipe → `vx_enqueue_draw` → CP → expand/setup/binning → RASTER（push 启动 FS）→ FS（`vx_tex4`/`vx_om4`）→ OM 写 framebuffer；并标出 SDK 边界与 `VORTEX_DRIVER=simx` 后端选择点。

**需要观察的现象**：测试输出 `device: …vortex…`（被 `check_run` 校验，[common.mk:119-122](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/vulkan/common.mk#L119-L122)），最后打印 `PASSED (triangle rendered, N/4096 pixels covered)`，退出码 0。

**预期结果**：三角形在 SimX 上正确渲染，像素覆盖数落在校验区间内；你画的图能解释每个 Vulkan 调用到设备侧 FF 单元的映射。

**待本地验证**：若未安装 mesa-vortex 工具链，`make run-simx` 会失败于找不到 mesa；此时完成步骤 1、2、4 的源码阅读与画图即可，并标注运行部分「待本地验证」。

## 6. 本讲小结

- Vortex 图形栈是**两棵树**：`mesa_vortex`（vortexpipe 驱动）以 SDK 形式单向消费本仓库（平台），靠 `$VORTEX_PATH` / `$VORTEX_HOME` 衔接，目标 north star 是 submit→present 全程设备常驻的「真 GPU」。
- `vortexpipe` 是 **llvmpipe 上的薄装饰器**：只覆盖特化入口，其余转发；回退契约是「逐次调用」判定，`$MESA_VORTEX_STRICT` 把静默回退变成可检测失败。
- 编译走 **Shape C**：NIR → `vp_nir_to_llvm`（标量 walker）→ LLVM-IR → `vp_compile_vxbin`（fork clang + `libvortex2.a`）→ `.vxbin`；图形 ISA 用 RISC-V custom-1 opcode，靠 funct3 区分 OM(2)/GETWS(4)/TEX(5)/window(6)/RTU(7)。
- 一次 `vkCmdDraw` 在硬件路径下是**一笔设备常驻事务**：VS 折进前端 stage 0，设备侧 sort-middle 前端产出 RASTER 缓冲，RASTER 以 FWD-5 **push** 模型自启动 run-once 的片段着色器。
- 一致性模型是 **inherit and accelerate**：继承 lavapipe 全 Vulkan 表面、加速子集、回退 CPU；纪律是「门控回退永远优于静默坍缩」。
- 测试侧 `tests/vulkan/common.mk` 用 `VK_ICD_FILENAMES` + `GALLIUM_DRIVER=vortexpipe` + `VORTEX_DRIVER=<后端>` + `check_run` 门控，把这条链路串起来并守住 STRICT 纪律。

## 7. 下一步学习建议

- **图形硬件栈细节**：本讲讲的是「软件/编译/驱动」视角；RASTER/TEX/OM 三个 FF 单元的硬件微架构、early-Z、fragment dispatch 与 SimX 模型，请读 [docs/designs/graphics_hardware_stack.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/graphics_hardware_stack.md)（对应 u10-l1 讲义）。
- **命令处理器与 KMU**：`vx_enqueue_draw` 在设备侧如何被 CP 解包、launch-barrier 如何排空，见 [docs/designs/command_processor.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/command_processor.md)（对应 u11-l3）。
- **光追在 Vulkan 里的落地**：`vp_nir_lower_ray_tracing_to_rtu.c` 把 Vulkan ray query 降级到 RTU 的 ISA-v2 窗口 op，配合 [docs/designs/ray_tracing_unit.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md)（对应 u10-l3），可继续追 `tests/vulkan/rtquery*`、`tests/vulkan/raytrace` 测试。
- **HIP 路径**：对比另一条上层 API 栈，下一讲 u12-l3 讲 chipStar 如何把 HIP 经 SPIR-V 跑在 Vortex 上。
