# AMD (HIP/ROCm) 后端

## 1. 本讲目标

本讲聚焦 Triton 的 AMD GPU 后端（基于 HIP/ROCm）。读完本讲，你应该能够：

1. 说出 AMD 后端的编译阶段管线，以及最终产物 `hsaco` 是怎么生成的。
2. 理解 AMD 用 `gfx` 名（如 `gfx942`、`gfx1100`、`gfx1170`、`gfx1250`）表示目标硬件，并能把这些名字映射到 CDNA / RDNA 等 ISA 家族。
3. 掌握 `TargetFeatures` 这一套「基于特性（feature）的门控」机制，理解它为何逐步取代「逐架构字符串硬编码」。
4. 对比 AMD 与 NVIDIA 后端在阶段名称、arch 表示方式、二进制扩展名上的差异。
5. 观察到本轮代码更新（`gfx1170` / RDNA4m、`gfx1250` multicast 拆分）是如何被纳入特性判定的。

## 2. 前置知识

本讲默认你已经学过 [u8-l1 后端发现与 BaseBackend 接口] 和 [u8-l2 NVIDIA 后端编译管线]，知道：

- 一个「后端（backend）」要实现 `BaseBackend` 接口，核心方法之一是 `add_stages`，它向编译器注册「阶段（stage）」字典；编译器按字典顺序依次执行这些阶段（见 [u5-l1 triton.compile：编译入口与阶段编排]）。
- `GPUTarget` 用三元组 `(backend, arch, warp_size)` 描述一块目标 GPU。
- NVIDIA 用「计算能力（compute capability）」如 `90` 表示 Hopper，最终二进制扩展名是 `cubin`。

下面几个 AMD/ROCm 专有名词先建立直觉：

- **HIP（Heterogeneous-Compute Interface for Portability）**：AMD 提供的一套类 CUDA 的运行时 API。Triton 在 AMD 上复用了大量与 CUDA 相似的接口形态（例如 `hipDeviceptr_t`、`torch.cuda`），因此 AMD 后端的很多代码「长得像」NVIDIA 后端。
- **ROCm**：AMD 的 GPU 计算栈（驱动 + 运行时 + 编译工具链），HIP 是其中的编程模型。
- **gfx 名**：AMD GPU 的架构代号，如 `gfx942`（MI300 系列）、`gfx1100`（RDNA3 消费卡）、`gfx1170`（RDNA4m）、`gfx1200`（RDNA4）、`gfx1250`（新一代）。编译器据此选择指令集（ISA）。
- **CDNA / RDNA**：AMD 两条 GPU 产品线。CDNA 面向计算（MI 系列，warp_size=64），RDNA 面向图形与消费级（warp_size=32）。
- **hsaco（HSACO）**：AMD 设备码二进制格式，相当于 NVIDIA 的 `cubin`，是最终被驱动加载执行的产物。
- **TDM（Tensor Descriptor Memory）**：`gfx1250` 引入的硬件张量描述符机制，可加速访存（后续会反复出现）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [third_party/amd/backend/compiler.py](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py) | AMD 编译后端主体：`HIPBackend`、`HIPOptions`、各编译阶段（`make_ttir/ttgir/llir/amdgcn/hsaco`）与一组架构判定辅助函数。 |
| [third_party/amd/backend/driver.py](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/driver.py) | AMD 驱动层：`HIPDriver`、`HIPLauncher`、`HIPUtils`，负责发现 HIP 运行时动态库、加载 hsaco、启动 kernel、构造 `GPUTarget`。 |
| [third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp) | C++ 侧的 `TargetFeatures` 实现：把 gfx 名解析为 `ISAFamily`，集中回答「这块硬件支持哪些特性」。 |
| [third_party/amd/include/Dialect/TritonAMDGPU/IR/TargetFeatures.h](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/include/Dialect/TritonAMDGPU/IR/TargetFeatures.h) | `TargetFeatures` 与 `ISAFamily` 枚举的头文件声明。 |
| [third_party/amd/lib/TritonAMDGPUToLLVM/Utility.cpp](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/TritonAMDGPUToLLVM/Utility.cpp) | `emitCtaMulticastMask` 等 lowering 工具函数，本轮更新中新增了 multicast 子组拆分逻辑。 |
| [third_party/nvidia/backend/compiler.py](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/nvidia/backend/compiler.py) | NVIDIA 后端，用于与本讲做横向对比。 |

## 4. 核心概念与源码讲解

### 4.1 AMD 后端的编译阶段与 hsaco 产物

#### 4.1.1 概念说明

和 NVIDIA 后端一样，AMD 后端通过 `add_stages` 注册一组编译阶段。区别在于：

- 阶段名称不同：AMD 的后段是 `llir → amdgcn → hsaco`（NVIDIA 是 `llir → ptx → cubin`）。
- `amdgcn` 是 AMD GPU 的汇编文本（相当于 NVIDIA 的 PTX 文本），`hsaco` 是汇编链接后的设备二进制（相当于 `cubin`）。
- AMD 还多了一条 Gluon 语言入口（`gluon_to_ttgir`）。

`add_stages` 的实现把每个阶段名映射到一个 lambda，lambda 接收上一阶段的 IR 并返回本阶段产物：

```python
# third_party/amd/backend/compiler.py:585-595
def add_stages(self, stages, options, language):
    if language == Language.TRITON:
        stages["ttir"]  = lambda src, metadata: self.make_ttir(src, metadata, options)
        stages["ttgir"] = lambda src, metadata: self.make_ttgir(src, metadata, options)
    elif language == Language.GLUON:
        stages["ttgir"] = lambda src, metadata: self.gluon_to_ttgir(src, metadata, options)
    stages["llir"]   = lambda src, metadata: self.make_llir(src, metadata, options)
    stages["amdgcn"] = lambda src, metadata: self.make_amdgcn(src, metadata, options)
    stages["hsaco"]  = lambda src, metadata: self.make_hsaco(src, metadata, options)
    if knobs.runtime.add_stages_inspection_hook is not None:
        knobs.runtime.add_stages_inspection_hook(self, stages, options, language, None)
```

链接：[third_party/amd/backend/compiler.py:585-595](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L585-L595)。这段代码把 `ttir`/`ttgir`/`llir`/`amdgcn`/`hsaco` 五个阶段挂进 `stages` 字典，编译器随后按 key 顺序逐级执行。最后的 `add_stages_inspection_hook` 是插件扩展点（见 [u12-l1 MLIR 插件 pass]），允许外部覆盖或插入阶段。

> 说明：`add_stages` 里没有显式写 `num_warps` 之类的分发——所有架构相关的差异都隐藏在各个 `make_*` 函数内部对 `options.arch` 的判定里。这也是为什么「新增一个 gfx 架构」时，改动往往集中在这些判定函数上（见 4.5 节）。

#### 4.1.2 核心流程

AMD 后端把一条 kernel 从 TTIR 编译到 hsaco 的主线是：

```text
TTIR  ──make_ttir──▶  TTGIR  ──make_ttgir──▶  LLIR  ──make_llir──▶  (LLVM IR 文本)
        │ 逻辑层优化         │ GPU 布局 + 优化      │ lowering + 设置 amdgpu 属性
        ▼                    ▼                    ▼
  在 MLIR 内部跑 pass    选 MMA / 流水线 / 布局   调 LLVM 把 MLIR LLVM-dialect 转 真·LLVM IR

   LLVM IR 文本 ──make_amdgcn──▶ amdgcn 汇编文本 ──make_hsaco──▶ hsaco 二进制
                  translate_to_asm                   assemble_amdgcn + link_hsaco
```

关键点：

- `make_ttir` / `make_ttgir` / `make_llir` 都在 MLIR 语境内，用 `ir.pass_manager()` 构造 pass 管线后 `pm.run()`。
- `make_llir` 末尾把 MLIR 的 LLVM-dialect 真正转成 LLVM 模块对象（`llvm.to_module`），并大量设置 `amdgpu-*` 函数属性，再用 `llvm.optimize_module` 做 O3 优化，最后 `str(llvm_mod)` 得到 LLVM IR 文本。
- `make_amdgcn` 把 LLVM IR 文本翻译成 amdgcn 汇编文本（`llvm.translate_to_asm`）。
- `make_hsaco` 把汇编文本汇编并链接成二进制（`amd.assemble_amdgcn` + `amd.link_hsaco`）。

#### 4.1.3 源码精读

先看产物的「身份证」：`HIPBackend.__init__` 把二进制扩展名设为 `hsaco`，`get_target_name` 返回形如 `hip:gfx942` 的目标名。

[third_party/amd/backend/compiler.py:147-153](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L147-L153) —— `binary_ext = "hsaco"`、`get_target_name` 返回 `hip:{arch}`。注意与 NVIDIA 的 `cuda:{capability}`（如 `cuda:90`）对照：AMD 直接用 gfx 字符串当 arch。

`make_hsaco` 负责最后一步，汇编 + 链接：

```python
# third_party/amd/backend/compiler.py:568-583
@staticmethod
def make_hsaco(src, metadata, options):
    target_features = []
    if knobs.compilation.enable_asan:
        target_features.append('+xnack')
    if true16 := disable_real_true16_feature(options.arch):
        target_features.append(true16)
    hsaco = amd.assemble_amdgcn(src, options.arch, ','.join(target_features))
    with tempfile.NamedTemporaryFile() as tmp_out:
        with tempfile.NamedTemporaryFile() as tmp_in:
            with open(tmp_in.name, "wb") as fd_in:
                fd_in.write(hsaco)
            amd.link_hsaco(tmp_in.name, tmp_out.name)
        with open(tmp_out.name, "rb") as fd_out:
            ret = fd_out.read()
    return ret
```

链接：[third_party/amd/backend/compiler.py:568-583](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L568-L583)。`amd.assemble_amdgcn` 把汇编文本编成对象文件，`amd.link_hsaco` 再做链接，产出可加载的 hsaco 字节串。`disable_real_true16_feature`（见下方 4.5）会在 gfx11 系列上注入 `-real-true16` 关闭某个尚不稳定的特性。

而 `make_llir` 是最长、最关键的阶段——它做完 lowering 后，给 kernel 函数挂上一堆 `amdgpu-` 属性，这些属性直接影响 LLVM 的代码生成质量。摘取设置调用约定、cluster 维度、warp 数、调度策略的一段：

```python
# third_party/amd/backend/compiler.py（节选自 make_llir）
kernel_fn.set_calling_conv(amd.CALLING_CONV_AMDGPU_KERNEL)
kernel_fn.add_fn_attr("amdgpu-cluster-dims", f"{cluster_dim},1,1")
kernel_fn.add_fn_attr("amdgpu-flat-work-group-size", f"1,{total_warps_num*options.warp_size}")
kernel_fn.add_fn_attr("uniform-work-group-size", "true")
if options.waves_per_eu != 0:
    kernel_fn.add_fn_attr("amdgpu-waves-per-eu", f"{options.waves_per_eu},{options.waves_per_eu}")
if is_coexec_scheduler_enabled(options.arch) and options.num_warps <= 4:
    kernel_fn.add_fn_attr("amdgpu-sched-strategy", "coexec")
```

链接：[third_party/amd/backend/compiler.py:445-475](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L445-L475)。可以看到：`num_ctas` 被翻译成 `amdgpu-cluster-dims`（多 CTA/集群启动）；warp 数 × warp_size 变成 `amdgpu-flat-work-group-size`；`coexec` 调度策略仅在 `gfx1250` 上启用（见 `is_coexec_scheduler_enabled`）。这就是「arch 字符串如何驱动差异化代码生成」的典型样例。

#### 4.1.4 代码实践（源码阅读型）

> 实践目标：理清 hsaco 产物的链路，并把「AMD 二进制扩展名」固定到记忆里。
>
> 1. 打开 `third_party/amd/backend/compiler.py`，定位 `add_stages`（约 585 行），确认阶段顺序为 `ttir → ttgir → llir → amdgcn → hsaco`。
> 2. 跳到 `make_amdgcn`（约 535 行），找到 `amd.assemble_amdgcn` 的上游 `llvm.translate_to_asm`；再跳到 `make_hsaco`（约 568 行），找到 `amd.link_hsaco`。
> 3. 在 [u5-l2 CompiledKernel] 学过编译产物存在 `kernel.asm` 字典里——记下 AMD 的产物键是 `ttir / ttgir / llir / amdgcn / hsaco`，而 NVIDIA 是 `ttir / ttgir / llir / ptx / cubin`。
>
> 需要观察的现象（待本地验证）：若你有 AMD GPU 并设置 `MLIR_ENABLE_DUMP=1` 或 `AMDGCN_ENABLE_DUMP=1`，应能在日志/输出里看到各级 IR 与「AMDGCN Dump」文本。
>
> 预期结果：你能口头复述「amdgcn 对应 PTX、hsaco 对应 cubin」这一对应关系。

#### 4.1.5 小练习与答案

**练习 1**：为什么 AMD 后端的 `make_hsaco` 要分 `assemble_amdgcn` 和 `link_hsaco` 两步，而不是一步到位？
**参考答案**：汇编只把文本翻译成对象码，但设备码还可能引用运行时库符号（如 `ocml`/`ockl` 里的数学函数）；`link_hsaco` 负责把这些外部引用解析并产生可被驱动加载的完整 hsaco。这与 NVIDIA `ptxas` 一步生成 cubin 的流程对应，只是 AMD 把「汇编」和「链接」拆开了。

**练习 2**：`add_stages` 里 `Language.TRITON` 与 `Language.GLUON` 分支注册的阶段有何不同？
**参考答案**：TRITON 语言注册 `ttir` 和 `ttgir` 两个前端阶段（从 AST/TTIR 进入）；Gluon 语言只注册 `ttgir`（用 `gluon_to_ttgir` 直接生成 TTGIR，跳过 TTIR）。两者都共享后续的 `llir/amdgcn/hsaco`。

---

### 4.2 gfx 架构映射：从字符串到 ISA Family

#### 4.2.1 概念说明

NVIDIA 用一个整数「计算能力」（如 `90` 表示 Hopper）来选架构，简单直接。AMD 则用形如 `gfx942`、`gfx1100`、`gfx1170`、`gfx1250` 的字符串。这些字符串的编码规则是：

```text
gfx  <major>  <minor>  <patch>
       1~2 位   1 位     1 位(十六进制)
```

例如 `gfx942` = major `9` / minor `4` / patch `2`（MI300，CDNA3）；`gfx1170` = major `11` / minor `7` / patch `0`（本轮新增，RDNA4m）。

问题是：raw 字符串不便于在 C++ 编译器里做条件分支（你不能写 `if (arch == "gfx1170" || arch == "gfx1100" ...)` 满天飞）。因此 Triton 在 C++ 侧定义了一个 `ISAFamily` 枚举，把 gfx 字符串「翻译」成几个粗粒度的 ISA 家族，后续所有特性判定都基于家族而非原始字符串。这就是「特性门控（feature gating）」要解决的核心问题。

#### 4.2.2 核心流程

`TargetFeatures` 把字符串映射成家族的流程：

```text
gfx 字符串(来自 ModuleOp 的 "triton.gpu.target" 属性, 形如 "hip:gfx1170")
   │  drop_front("hip:")
   ▼
parseGfxArch()  ──拆出 major/minor/patch──▶  GfxArch{11,7,0}
   │
   ▼
getISAFamily()  ──按 major/minor/patch 查表──▶  ISAFamily::RDNA4m
```

家族划分（截至当前 HEAD）：

| ISAFamily | 典型 gfx 名 | 产品线 | warp_size |
| --- | --- | --- | --- |
| `CDNA1/CDNA2` | gfx803/gfx906 | 较老计算卡 | 64 |
| `CDNA3` | gfx942 | MI300 | 64 |
| `CDNA4` | gfx950 | MI350 系列 | 64 |
| `GCN5_1` | gfx906（旧路径） | — | 64 |
| `RDNA1/RDNA2` | gfx1010/gfx1030 | 消费卡 | 32 |
| `RDNA3` | gfx1100/gfx1150 | RDNA3 消费卡 | 32 |
| **`RDNA4m`** | **gfx1170（新增）** | RDNA4m | 32 |
| `RDNA4` | gfx1200 | RDNA4 | 32 |
| `GFX1250` | gfx1250 | 新一代（含 TDM/cluster） | 32 |

#### 4.2.3 源码精读

先看字符串解析器 `parseGfxArch`，它手工拆出 major/minor/patch（注意 patch 用十六进制解析，minor/major 用十进制）：

```cpp
// third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:17-39
std::optional<GfxArch> parseGfxArch(StringRef arch) {
  if (!arch.consume_front("gfx")) return std::nullopt;
  if (arch.size() < 3) return std::nullopt;

  unsigned patch;
  if (arch.take_back(1).getAsInteger(16, patch)) return std::nullopt;
  arch = arch.drop_back();

  unsigned minor;
  if (arch.take_back(1).getAsInteger(10, minor)) return std::nullopt;
  arch = arch.drop_back();

  unsigned major;
  if (arch.getAsInteger(10, major)) return std::nullopt;

  return GfxArch{major, minor, patch};
}
```

链接：[third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:17-39](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp#L17-L39)。它从尾部逐个取字符：最后 1 位是 patch（hex），倒数第 2 位是 minor（dec），剩余是 major（dec）。

再看家族查表，本轮新增的 `RDNA4m` 分支就在这里：

```cpp
// third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:88-98
  // RDNA ISA cases.
  if (major == 12 && minor == 0)
    return ISAFamily::RDNA4;
  if (major == 11 && minor == 7)
    return ISAFamily::RDNA4m;        // ← 本轮新增
  if (major == 11)
    return ISAFamily::RDNA3;
  if (major == 10 && minor == 3)
    return ISAFamily::RDNA2;
  if (major == 10 && minor == 1)
    return ISAFamily::RDNA1;
```

链接：[third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:88-98](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp#L88-L98)。注意顺序很关键：必须先判 `minor == 7`（RDNA4m）再判「所有 major==11 落 RDNA3」，否则 gfx1170 会被 `if (major == 11)` 提前归入 RDNA3。

枚举本身在头文件里（本轮新增 `RDNA4m`）：

```cpp
// third_party/amd/include/Dialect/TritonAMDGPU/IR/TargetFeatures.h:12-25
enum class ISAFamily {
  Unknown, GCN5_1, CDNA1, CDNA2, CDNA3, CDNA4,
  RDNA1, RDNA2, RDNA3, RDNA4m, RDNA4, GFX1250,
};
```

链接：[third_party/amd/include/Dialect/TritonAMDGPU/IR/TargetFeatures.h:12-25](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/include/Dialect/TritonAMDGPU/IR/TargetFeatures.h#L12-L25)。

warp_size 也由家族决定（CDNA/GCN 走 64，其余 32）：

```cpp
// third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:123-134
int TargetFeatures::getWarpSize() const {
  switch (getISAFamily()) {
  case ISAFamily::GCN5_1: case ISAFamily::CDNA1: case ISAFamily::CDNA2:
  case ISAFamily::CDNA3: case ISAFamily::CDNA4:
    return 64;
  default:
    return 32;
  }
}
```

链接：[third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:123-134](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp#L123-L134)。这与 Python 侧 `HIPOptions.__post_init__` 的 `warp_size = 32 if gfx_major >= 10 else 64`（[compiler.py:114-116](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L114-L116)）一致——RDNA（gfx10+）warp=32，CDNA（gfx9）warp=64。

Python 侧构造 `GPUTarget` 的地方在 driver.py：

```python
# third_party/amd/backend/driver.py:396-401
def get_current_target(self):
    device = self.get_current_device()
    device_properties = self.utils.get_device_properties(device)
    arch = knobs.runtime.override_arch or device_properties['arch']
    warp_size = device_properties['warpSize']
    return GPUTarget("hip", arch.split(':')[0], warp_size)
```

链接：[third_party/amd/backend/driver.py:396-401](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/driver.py#L396-L401)。`device_properties['arch']` 形如 `gfx1170:gfx1170:sram-ecc-...`，用 `split(':')[0]` 取干净的 `gfx1170`。这个 arch 字符串随后会经 `options.arch` 一路传到各个判定函数与 `TargetFeatures`。

#### 4.2.4 代码实践

> 实践目标：手工验证 gfx 字符串到家族的映射，理解 `parseGfxArch` 的位序。
>
> 1. 在纸上对 `gfx1170` 应用 `parseGfxArch`：去掉 `gfx` → `1170`；取末位 `0` 作 patch（hex→0）；剩 `117` 取末位 `7` 作 minor；剩 `11` 作 major。得到 `{major=11, minor=7}`。
> 2. 代入 `getISAFamily` 查表：`major==11 && minor==7` 命中 `RDNA4m`。
> 3. 同理验证 `gfx942`→`{9,4,2}`→`CDNA3`；`gfx1250`→`{12,5,0}`→`GFX1250`；`gfx1100`→`{11,0,0}`→`RDNA3`。
>
> 需要观察的现象：若把第 2 步的 `RDNA4m` 分支删掉，`gfx1170` 会落到哪？
>
> 预期结果：会落到下一条 `if (major == 11) return RDNA3;`，被误判为 RDNA3——这正是分支顺序必须「先具体后宽泛」的原因。

#### 4.2.5 小练习与答案

**练习 1**：`GPUTarget` 的 arch 用 `arch.split(':')[0]` 处理，为什么 arch 里会有冒号？
**参考答案**：AMD 设备属性返回的 arch 字符串是复合形式，如 `gfx1170:gfx1170:sram-ecc-...`，冒号后跟复制信息与特性标志。Triton 只关心主架构名，所以取第一段。

**练习 2**：为什么 `getISAFamily` 里 `RDNA4m`（minor==7）的判断必须写在 `if (major == 11) return RDNA3;` 之前？
**参考答案**：因为 RDNA4m 与 RDNA3 的 major 都是 11，若先命中宽泛的 `major==11` 分支，gfx1170 会被错误归为 RDNA3，导致后续走错特性分支（如 MMA 版本、cache 策略）。

---

### 4.3 TargetFeatures：基于特性的门控机制

#### 4.3.1 概念说明

早期的 Triton AMD 后端大量出现 `if (arch == "gfx942") ...`、`if (arch == "gfx1100") ...` 这种「逐架构字符串硬编码」。问题很明显：

1. 每加一个新架构（如本轮的 gfx1170），都要在几十处地方补字符串。
2. 容易遗漏；语义相近的架构（如 RDNA3/RDNA4m）行为基本一致却被分别列举。

`TargetFeatures` 的思路是：把「这块硬件支持什么」抽成一组布尔/数值查询方法（`supportsTDM()`、`supportsMultiCTALaunch()`、`getMaxMulticastMaskPopcount()` 等），让上层 lowering 代码只问「你支不支持这个特性」，而不关心「你是哪块卡」。新增架构时，只要它属于已有家族（如 gfx1170 属 RDNA4m，行为接近 RDNA4），往往只需在家族枚举里加一项、在若干 `switch` 里补一个 `case`，而不必改动调用方。

#### 4.3.2 核心流程

```text
ModuleOp 的 "triton.gpu.target" 属性 = "hip:gfx1250"
        │  TargetFeatures::fromModuleOp()
        ▼
   TargetFeatures(arch="gfx1250")
        │  上层 lowering 调用查询方法
        ▼
   supportsTDM()          → true   (只有 GFX1250 支持 TDM)
   supportsMultiCTALaunch()→ true
   getMaxMulticastMaskPopcount() → 5  (GFX1250 可设 5 位 multicast mask)
                                  → 1  (其它架构)
```

#### 4.3.3 源码精读

`fromModuleOp` 从 MLIR 模块属性里取出 arch 并构造 `TargetFeatures`：

```cpp
// third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:47-58
TargetFeatures TargetFeatures::fromModuleOp(ModuleOp moduleOp) {
  auto targetAttr = moduleOp->getAttrOfType<StringAttr>(triton::gpu::AttrTargetName);
  if (!targetAttr) return TargetFeatures(StringRef());
  StringRef targetName = targetAttr.getValue();
  assert(targetName.starts_with(kTargetPrefix) &&  // "hip:"
         "expected target attribute to be prefixed with \"hip:\"");
  return TargetFeatures(targetName.drop_front(sizeof(kTargetPrefix) - 1));
}
```

链接：[third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:47-58](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp#L47-L58)。这正是 Python 侧 `make_ttgir` 里 `passes.ttir.add_convert_to_ttgpuir(pm, f"hip:{options.arch}", ...)` 写进模块的那个 target 属性的「读出端」。

本轮新增的两个查询方法（TDM 与多 CTA 仅 GFX1250 支持；multicast mask 上限也是按 GFX1250 区分）：

```cpp
// third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:240-246
bool TargetFeatures::supportsTDM() const { return isGFX1250(); }
bool TargetFeatures::supportsMultiCTALaunch() const { return isGFX1250(); }

unsigned TargetFeatures::getMaxMulticastMaskPopcount() const {
  return isGFX1250() ? 5 : 1;     // ← 本轮新增
}
```

链接：[third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp:240-246](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp#L240-L246)。

「门控」的好处在新架构接入时体现得最清楚。以本轮为例，gfx1170（RDNA4m）的行为和 RDNA4 高度相似，因此各 lowering 文件只需在既有 `switch` 里补一行 `case ISAFamily::RDNA4m:`，让它「跟着 RDNA4 走」。三处典型例子：

- WMMA（矩阵乘）版本选择：RDNA4m 与 RDNA4 同用版本 2：

```cpp
// third_party/amd/lib/TritonAMDGPUTransforms/AccelerateAMDMatmul.cpp:53-62
int getWmmaVersion(ISAFamily isaFamily) {
  switch (isaFamily) {
  case ISAFamily::RDNA3: return 1;
  case ISAFamily::RDNA4m:   // ← 本轮新增，与 RDNA4 同
  case ISAFamily::RDNA4:  return 2;
  case ISAFamily::GFX1250: return 3;
  ...
```

链接：[third_party/amd/lib/TritonAMDGPUTransforms/AccelerateAMDMatmul.cpp:53-62](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/TritonAMDGPUTransforms/AccelerateAMDMatmul.cpp#L53-L62)。

- 缓存策略：RDNA4m 沿用 RDNA3 的 cache ctrl 计算函数：

```cpp
// third_party/amd/lib/TritonAMDGPUToLLVM/Utility.cpp:719-724
  case ISAFamily::RDNA3:
  case ISAFamily::RDNA4m:   // ← 本轮新增
    return getCtrlBitsForCacheModifierOnRDNA3(cm, isLoad);
```

链接：[third_party/amd/lib/TritonAMDGPUToLLVM/Utility.cpp:719-724](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/TritonAMDGPUToLLVM/Utility.cpp#L719-L724)。

- 共享内存 load/store tile、定时器、buffer resource descriptor flags 等处也都只是追加 `case ISAFamily::RDNA4m:`（见 `TargetInfo.cpp`、`BufferOpsEmitter.cpp` 的本轮 diff）。

这就是「门控」相对「字符串硬编码」的核心收益：**新增架构 = 在家族表加一项 + 在相关 switch 补 case，调用方零改动**。

`getMaxMulticastMaskPopcount` 的作用：GFX1250 硬件允许一个 multicast mask 设置最多 5 个 bit（即一次广播给 5 个 workgroup），其它架构只允许 1 个 bit。若集群布局要求共享的 CTA 数超过这个上限，lowering 需要把组拆成更小的子组——这正是 4.5 节的 multicast 拆分。

#### 4.3.4 代码实践

> 实践目标：体会「问特性」与「问架构」两种写法的差别。
>
> 1. 在 `TargetFeatures.cpp` 中数一下有多少查询方法是 `return isGFX1250();` 或 `return llvm::is_contained({...}, getISAFamily());` 形式（如 `supportsTDM`、`supportsMultiCTALaunch`、`supportsBufferAtomicRMW`）。
> 2. 设想：如果上层 lowering 代码直接写 `if (arch == "gfx1250")` 而不是 `if (targetFeatures.supportsTDM())`，当出现第二个支持 TDM 的架构时，要改多少处？
>
> 需要观察的现象：统计本轮 diff 中 `case ISAFamily::RDNA4m:` 新增出现的次数（约 5~6 处），都是「补一行 case」即可，调用方未改。
>
> 预期结果：你能用一句话说明「门控把架构差异收敛到了 `TargetFeatures` 一个类里」。

#### 4.3.5 小练习与答案

**练习 1**：`supportsMultiCTALaunch()` 只在 GFX1250 返回 true，这与 Python 侧哪个检查呼应？
**参考答案**：呼应 `compiler.py` 的 `parse_options` 里 `amd.supports_multi_cta_launch(self.target.arch)` 检查（[compiler.py:163-164](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L163-L164)）：非 GFX1250 上设 `num_ctas > 1` 会被直接拒绝。

**练习 2**：为什么 `getMaxMulticastMaskPopcount` 要做成「GFX1250 返回 5，其它返回 1」，而不是统一返回一个大值？
**参考答案**：multicast mask 的位宽是硬件约束。GFX1250 之前的硬件本质上不支持多 CTA multicast，因此返回 1（等于不做 multicast）；硬塞更多位会被硬件忽略，反而让布局优化做出错误假设。把上限封装在 `TargetFeatures` 里，调用方就不必记这些硬件细节。

---

### 4.4 AMD 与 NVIDIA 后端的对比

#### 4.4.1 概念说明

学完 NVIDIA（[u8-l2]）再看 AMD，会发现两者「同构」但处处「对偶」：都实现 `BaseBackend`，都注册一组 stage，都有 driver/launcher；但目标硬件的语言（PTX vs amdgcn）、二进制（cubin vs hsaco）、arch 表示（capability 整数 vs gfx 字符串）、warp_size（恒 32 vs 32/64）都不同。理解这种「对偶」能让你快速在两个后端间迁移知识。

#### 4.4.2 核心流程（对比表）

| 维度 | NVIDIA | AMD |
| --- | --- | --- |
| 后端名 / `GPUTarget.backend` | `cuda` | `hip` |
| arch 表示 | 计算能力整数，如 `90`（Hopper） | gfx 字符串，如 `gfx942`、`gfx1250` |
| `get_target_name` | `cuda:{capability}` | `hip:{arch}` |
| 前端阶段（TRITON） | `make_ttir` / `make_ttgir` | `make_ttir` / `make_ttgir`（名字相同，内部 pass 不同） |
| 后段阶段 | `make_llir` → `make_ptx` → `make_cubin` | `make_llir` → `make_amdgcn` → `make_hsaco` |
| 汇编文本 | PTX | amdgcn |
| 二进制扩展名 (`binary_ext`) | `cubin` | `hsaco` |
| 二进制生成工具 | 外部 `ptxas`（subprocess） | 内置 `amd.assemble_amdgcn` + `amd.link_hsaco` |
| warp_size | 恒 32 | CDNA=64，RDNA/GFX1250=32 |
| 架构差异化方式 | `capability // 10` 分支（8/9/10） | gfx 字符串判定 + C++ `ISAFamily` 门控 |
| MMA 加速 pass | `passes.ttgpuir.add_accelerate_matmul` | `amd.passes.ttgpuir.add_accelerate_matmul`（含 WMMA 版本选择） |
| warp 特化 | Hopper/Blackwell 的 warpspec | AMD 侧 `add_warp_pipeline*`（不同机制） |

#### 4.4.3 源码精读

对照两份 `add_stages`：

AMD（[compiler.py:585-595](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L585-L595)，已在上文贴出）注册 `ttir/ttgir/llir/amdgcn/hsaco`。

NVIDIA：

```python
# third_party/nvidia/backend/compiler.py:594-605
def add_stages(self, stages, options, language):
    capability = self._parse_arch(options.arch)
    if language == Language.TRITON:
        stages["ttir"]  = lambda src, metadata: self.make_ttir(src, metadata, options, capability)
        stages["ttgir"] = lambda src, metadata: self.make_ttgir(src, metadata, options, capability)
    elif language == Language.GLUON:
        stages["ttgir"] = lambda src, metadata: self.gluon_to_ttgir(src, metadata, options, capability)
    stages["llir"] = lambda src, metadata: self.make_llir(src, metadata, options, capability)
    stages["ptx"]  = lambda src, metadata: self.make_ptx(src, metadata, options, self.target.arch)
    stages["cubin"]= lambda src, metadata: self.make_cubin(src, metadata, options, self.target.arch)
    ...
```

链接：[third_party/nvidia/backend/compiler.py:594-605](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/nvidia/backend/compiler.py#L594-L605)。

注意一个细微差别：NVIDIA 的每个 `make_*` 都额外带 `capability` 参数（因为 arch 在 Python 里是 `sm90` 字符串，需要 `_parse_arch` 解析成 int）；AMD 的 `make_*` 直接用 `options.arch`（gfx 字符串），不需要这层解析。`binary_ext` 两边分别设为 `cubin`（[nvidia/compiler.py:185](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/nvidia/backend/compiler.py#L185)）与 `hsaco`（[amd/compiler.py:150](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L150)）。

`make_ttgir` 内部分支方式也不同：NVIDIA 按 `capability // 10` 三选一（[nvidia/compiler.py:283-309](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/nvidia/backend/compiler.py#L283-L309)，8=Ampere / 9=Hopper / ≥10=Blackwell）；AMD 则用一组布尔辅助函数（`is_async_copy_enabled`、`is_pingpong_schedule_enabled`、`is_in_thread_transpose_enabled` 等，见 4.5）组合决策，更细粒度。

#### 4.4.4 代码实践（对照阅读型）

> 实践目标：用「并排阅读」两份 `compiler.py` 建立对偶映射。
>
> 1. 同时打开 `third_party/nvidia/backend/compiler.py` 与 `third_party/amd/backend/compiler.py`，定位各自的 `add_stages`。
> 2. 画一张两列对照表：左列写 NVIDIA 的 5 个阶段名，右列写 AMD 对应阶段名；标出「ptx↔amdgcn、cubin↔hsaco」这条对应线。
> 3. 解释 arch 表示方式的不同：NVIDIA 用 `sm{capability}` 经 `_parse_arch` → int `capability`；AMD 直接用 gfx 字符串，最终由 C++ `TargetFeatures` 二次解析。
>
> 需要观察的现象：两边的 `add_stages` 末尾都调用 `knobs.runtime.add_stages_inspection_hook`，说明插件扩展点是跨后端统一的。
>
> 预期结果：你能不看资料说出「把 NVIDIA 的 ptx+cubin 换成 amdgcn+hsaco，把 capability 换成 gfx 名」就得到 AMD 的轮廓。

#### 4.4.5 小练习与答案

**练习 1**：为什么 NVIDIA 的 `make_*` 需要 `capability` 参数而 AMD 不需要？
**参考答案**：NVIDIA 的 arch 是 `sm90` 字符串，下游 pass 需要的是整数 capability，所以提前用 `_parse_arch` 解析并传参；AMD 的下游 pass（Python 与 C++ 两端）都直接消费 gfx 字符串，C++ 端再由 `TargetFeatures` 内部解析，因此 Python 函数无需额外参数。

**练习 2**：两个后端的 `binary_ext` 分别用于什么？
**参考答案**：它告诉 `CompiledKernel`（[u5-l2]）把最终二进制产物以什么扩展名/类型存储与加载。NVIDIA 是 `cubin`，AMD 是 `hsaco`；驱动层据此调用对应的 `load_binary`。

---

### 4.5 机器相关的特性实践：in-thread transpose 与 multicast 拆分

本节把本轮更新（previous HEAD `23ca0e4` → current HEAD `d16541f8`）涉及的两个真实改动讲透，作为「新架构如何被纳入特性判定」的范例。

#### 4.5.1 概念说明

AMD 后端有一组「策略开关」辅助函数，它们读 `options.arch` 并结合 `knobs.amd.*` 旋钮决定是否启用某项优化。其中：

- **in-thread transpose**：一种把矩阵转置在寄存器线程内完成的优化，避免额外共享内存往返。它对部分架构有益、对部分架构反而有害，因此需要按架构开关。
- **multicast**（GFX1250 的 cluster load）：一次加载广播给集群内多个 CTA，节省显存带宽。受限于硬件 mask 位宽上限，超限需拆分。

#### 4.5.2 核心流程

in-thread transpose 的判定：

```text
is_in_thread_transpose_enabled(arch)
   ├─ 若用户设了 knobs.amd.use_in_thread_transpose(None 之外) → 用用户值
   └─ 否则 → arch 命中 [gfx942, gfx110*, gfx115*, gfx117*, gfx120*] 之一即为 true
```

multicast mask 的拆分（lowering 期，C++）：

```text
集群布局需要 N 个 CTA 共享一份数据
   │  free bits 数 = log2(N)
   ▼
若 free bits 数 > floor(log2(maxMaskPopcount))   (GFX1250: maxMaskPopcount=5 → 上限 3 个 free bit)
   │
   ▼
丢弃最高的若干 free bit（它们变成 subgroup 选择位）
   ▼
组被拆成更小的子组，每个子组 ≤ maxMaskPopcount 个 CTA，并 emit 一条 remark 提示
```

#### 4.5.3 源码精读

本轮在 `is_in_thread_transpose_enabled` 里新增了 `"gfx117" in arch` 一项（用子串匹配，可覆盖 `gfx1170`）：

```python
# third_party/amd/backend/compiler.py:27-29
def is_in_thread_transpose_enabled(arch):
    return (arch == "gfx942" or "gfx110" in arch or "gfx115" in arch
            or "gfx117" in arch or "gfx120" in arch) \
        if knobs.amd.use_in_thread_transpose is None else knobs.amd.use_in_thread_transpose
```

链接：[third_party/amd/backend/compiler.py:27-29](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L27-L29)。这是本轮 AMD 后端在 Python 侧的唯一改动：把新架构 gfx1170（RDNA4m）纳入 in-thread transpose 启用范围。该函数在 `make_ttgir` 中被调用（[compiler.py:297-299](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L297-L299)）。

> 设计观察：这里仍用「arch 字符串列表」而非 `TargetFeatures` 门控，属于尚未完成迁移的「遗留硬编码」。对比 4.3 节可知，理想做法是把它也下沉为 `TargetFeatures::supportsInThreadTranspose()`。这是阅读源码时识别「技术债」的好例子。

multicast 拆分的实现（`emitCtaMulticastMask`，本轮新增 `maxMaskPopcount` 参数与拆分逻辑）：

```cpp
// third_party/amd/lib/TritonAMDGPUToLLVM/Utility.cpp:394-429
Value emitCtaMulticastMask(RewriterBase &rewriter, Location loc, Value groupId,
                           const LinearLayout &regLayout,
                           unsigned maxMaskPopcount) {       // ← 本轮新增参数
  ...
  // A communication group spans 2^(number of free bits) CTAs, so we can only
  // keep floor(log2(limit)) free bits. Dropping the highest free bits turns
  // them into subgroup selectors.
  int maxFreeBits = llvm::Log2_32(maxMaskPopcount);
  int multicastFreeVarMask = freeVarMask;
  while (llvm::popcount<uint32_t>(multicastFreeVarMask) > maxFreeBits)
    multicastFreeVarMask ^= llvm::bit_floor<uint32_t>(multicastFreeVarMask);

  if (multicastFreeVarMask != freeVarMask) {
    unsigned numSharingCTAs = 1u << llvm::popcount<uint32_t>(freeVarMask);
    unsigned numMulticastCTAs = 1u << maxFreeBits;
    emitRemark(loc) << "Multicast group contains " << numSharingCTAs
                    << " workgroups, exceeding the hardware limit of "
                    << maxMaskPopcount << " ... splitting it into subgroups ...";
  }
  ...
```

链接：[third_party/amd/lib/TritonAMDGPUToLLVM/Utility.cpp:394-429](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/TritonAMDGPUToLLVM/Utility.cpp#L394-L429)。逻辑：用 `Log2_32(maxMaskPopcount)` 算出能保留多少个 free bit；超出的最高位被「降级」为 subgroup 选择位（`bit_floor` 逐位异或去掉），从而把大组拆成不超限的子组，并发出一条 remark 告知用户。

调用方把硬件上限透传进来（本轮改动的联动点）：

```cpp
// third_party/amd/lib/TritonAMDGPUToLLVM/LoadStoreOpToLLVM.cpp:590-595
        multicastMask = LLVM::AMD::emitCtaMulticastMask(
            rewriter, loc, clusterCTAId, regLayout,
            targetInfo.getMaxMulticastMaskPopcount());   // ← 本轮新增实参
```

链接：[third_party/amd/lib/TritonAMDGPUToLLVM/LoadStoreOpToLLVM.cpp:590-595](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/TritonAMDGPUToLLVM/LoadStoreOpToLLVM.cpp#L590-L595)。`targetInfo.getMaxMulticastMaskPopcount()` 最终调用 4.3 节的 `TargetFeatures::getMaxMulticastMaskPopcount()`（GFX1250→5，其它→1）。这条链路完整展示了「TargetFeatures 门控 → TargetInfo 透传 → lowering 使用」的分层。

另一处本轮修复：`LoadStoreOpToLLVM.cpp` 的 `emitFence` 修正了 buffer atomics 的 release/acquire fence 顺序交换 bug（[LoadStoreOpToLLVM.cpp:149-152](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/TritonAMDGPUToLLVM/LoadStoreOpToLLVM.cpp#L149-L152)），与特性门控无直接关系，但属于本轮 AMD 后端的正确性修复，供你了解。

#### 4.5.4 代码实践（对应讲义规格的实践任务）

> **实践目标**：对比 NVIDIA 与 AMD 两个 `compiler.py` 的 `add_stages`，列出阶段名称与最终二进制扩展名差异；并在 `is_in_thread_transpose_enabled` 中观察 gfx117 等新架构如何被纳入特性判定。
>
> **操作步骤**：
> 1. 并排打开 [nvidia/compiler.py:594-605](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/nvidia/backend/compiler.py#L594-L605) 与 [amd/compiler.py:585-595](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L585-L595)，填写下表：
>
> | | NVIDIA | AMD |
> | --- | --- | --- |
> | 前端阶段 | ttir, ttgir | ttir, ttgir |
> | 后段阶段 | llir, **ptx**, **cubin** | llir, **amdgcn**, **hsaco** |
> | 最终二进制扩展名 | cubin | hsaco |
> | arch 表示 | compute capability（整数，如 90） | gfx 名（字符串，如 gfx1170） |
>
> 2. 解释 arch 表示方式的不同：NVIDIA 在 Python 里就把 `sm90` 解析成 int capability 传给各 `make_*`；AMD 把 gfx 字符串一路传到 C++ `TargetFeatures`，再解析成 `ISAFamily`。
> 3. 打开 [amd/compiler.py:27-29](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L27-L29)，确认 `is_in_thread_transpose_enabled` 的架构列表含 `"gfx117" in arch`（本轮新增），它通过子串匹配覆盖 gfx1170。再追到 `make_ttgir` 里它的调用点（约 297 行），看其返回值如何决定是否运行 `add_in_thread_transpose` pass。
> 4. （可选）用 `git log -p -1 -- third_party/amd/backend/compiler.py`（只读）查看本轮这条单行改动的提交信息，确认其属于 RDNA4m/gfx1170 支持。
>
> **需要观察的现象**：第 3 步中，若把 `"gfx117" in arch` 删掉，gfx1170 上的 in-thread transpose pass 会被跳过（行为退化）。
>
> **预期结果**：你能口头复述「AMD 后端新增 gfx 架构的两个落点：C++ 侧补 `ISAFamily` 枚举 + switch case（特性门控），Python 侧在策略函数里补 arch 子串（遗留硬编码）」。
>
> 说明：本实践为源码阅读型，无需 GPU，也无运行命令；如运行结果无法确定，记为「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`is_in_thread_transpose_enabled` 里 `knobs.amd.use_in_thread_transpose is None else ...` 这段三元表达式的含义是什么？
**参考答案**：它提供一个「用户覆盖」入口。若用户通过环境变量 `TRITON_HIP_USE_IN_THREAD_TRANSPOSE` 显式设了值（非 None），就用用户值强制开关；否则按架构默认策略（命中 gfx 列表则启用）。`is_async_copy_enabled`、`is_coexec_scheduler_enabled` 等都是同一模式。

**练习 2**：multicast mask 拆分为什么用 `Log2_32(maxMaskPopcount)` 而不是直接用 `maxMaskPopcount`？
**参考答案**：因为 free bit 数与 CTA 数是 2 的幂关系（k 个 free bit = \(2^k\) 个 CTA）。硬件允许的 mask 位数 `maxMaskPopcount`（如 5）不一定是 2 的幂，能容纳的最大 free bit 数是 \(\lfloor \log_2(\text{maxMaskPopcount}) \rfloor\)。所以用 `Log2_32` 把「位数上限」换算成「free bit 数上限」\( \text{maxFreeBits} = \lfloor \log_2 5 \rfloor = 2 \)，即 GFX1250 上每个子组最多 \(2^2=4\) 个 CTA（虽可设 5 位，但分组必须按 2 的幂）。数学上即：

\[
\text{maxFreeBits} = \lfloor \log_2(\text{maxMaskPopcount}) \rfloor
\]

**练习 3**：为什么说 `is_in_thread_transpose_enabled` 还停留在「字符串硬编码」、而 `getMaxMulticastMaskPopcount` 已经是「特性门控」？
**参考答案**：前者直接列举 arch 子串，新增架构要改函数体；后者返回 `isGFX1250() ? 5 : 1`，调用方（lowering）只问「上限是多少」，不关心架构名，新增第二个支持高上限的架构时只需改 `TargetFeatures` 一处。后者是推荐演进方向。

---

## 5. 综合实践

**任务：给 AMD 后端「虚拟新增一个架构」，走一遍完整的接入清单。**

假设 AMD 即将发布 `gfx1300`（虚构，用于练习），它属于 RDNA5 家族，warp_size=32，支持 in-thread transpose，但不支持 TDM/多 CTA launch。请基于本讲源码，列出要让 Triton 正确编译该架构需要改的点（只做源码阅读与方案设计，不真正改代码）：

1. **C++ 家族表**：在 [TargetFeatures.h:12-25](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/include/Dialect/TritonAMDGPU/IR/TargetFeatures.h#L12-L25) 的 `ISAFamily` 枚举加 `RDNA5`；在 [TargetFeatures.cpp:88-98](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/lib/Dialect/TritonAMDGPU/IR/TargetFeatures.cpp#L88-L98) 加 `if (major == 13) return ISAFamily::RDNA5;`，并把它加入 `isRDNA()` 的 switch。
2. **特性 switch**：审视 `AccelerateAMDMatmul.cpp`、`Utility.cpp`（cache ctrl）、`TargetInfo.cpp`（timer/tile）等处的 `switch(ISAFamily)`，决定 RDNA5 跟 RDNA4 还是 RDNA3 走，补 `case ISAFamily::RDNA5:`。由于它不支持 TDM/多 CTA，`supportsTDM()`/`supportsMultiCTALaunch()` 无需改（默认 false）。
3. **Python 策略函数**：在 [compiler.py:27-29](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L27-L29) 的 `is_in_thread_transpose_enabled` 里补 `"gfx130" in arch`。
4. **验证**：说明 `warp_size` 会自动正确——Python 侧 `HIPOptions.__post_init__` 用 `gfx_major >= 10` 判定（[compiler.py:114-116](https://github.com/triton-lang/triton/blob/d16541f83b135d4c9479be65897432207ed8f209/third_party/amd/backend/compiler.py#L114-L116)），gfx1300 的 major=13 ≥ 10，自动得 warp_size=32。

**预期结果**：你产出一份「改动清单」，能清楚区分哪些改动属于「特性门控层（C++ TargetFeatures）」、哪些属于「遗留硬编码层（Python 策略函数）」，并解释为什么前者更可维护。这个练习把本讲的「架构映射 + 特性门控 + 阶段管线」三个模块串了起来。

## 6. 本讲小结

- AMD 后端通过 `add_stages` 注册 `ttir → ttgir → llir → amdgcn → hsaco` 五阶段，`amdgcn` 对应 PTX、`hsaco` 对应 cubin，最终二进制扩展名是 `hsaco`。
- AMD 用 gfx 字符串（如 `gfx942`/`gfx1170`/`gfx1250`）表示架构，C++ 侧 `TargetFeatures::parseGfxArch` + `getISAFamily` 把它解析为 `ISAFamily`（CDNA/RDNA/GFX1250…），分支顺序必须「先具体后宽泛」。
- `TargetFeatures` 用一组查询方法（`supportsTDM`、`supportsMultiCTALaunch`、`getMaxMulticastMaskPopcount`…）实现「特性门控」，把架构差异收敛到一处，新增架构只需补 `case`，调用方零改动——本轮 gfx1170/RDNA4m 的接入正是如此。
- 本轮更新要点：① Python 侧 `is_in_thread_transpose_enabled` 新增 `"gfx117"`（覆盖 gfx1170）；② C++ 侧新增 `RDNA4m` 家族与多处 `case`；③ `getMaxMulticastMaskPopcount`（GFX1250→5，否则→1）配合 `emitCtaMulticastMask` 实现 multicast 子组拆分。
- 与 NVIDIA 后端处处「对偶」：同样的 `BaseBackend` 结构，但目标语言（PTX/amdgcn）、二进制（cubin/hsaco）、arch 表示（capability 整数/gfx 字符串）、warp_size（恒 32/32 或 64）不同。

## 7. 下一步学习建议

- **继续向下**：读 [u10-l3 TritonGPUToLLVM：lowering 到 LLVM IR]，再结合本讲看 AMD 专属的 `lib/TritonAMDGPUToLLVM/`（`LoadStoreOpToLLVM.cpp`、`Utility.cpp`、`BufferOpsEmitter.cpp`），理解 amdgcn 是如何逐条 lowering 出来的。
- **横向扩展**：对照本讲读 [u8-l2 NVIDIA 后端编译管线]，把两个后端的 `make_ttgir` 并排看，体会 `capability // 10` 分支与 AMD 布尔策略函数两种差异化方式的取舍。
- **深入门控**：把 `TargetFeatures.cpp` 里所有 `supports*` / `get*` 方法通读一遍，列出每个方法当前支持哪些 `ISAFamily`，作为「AMD 硬件能力矩阵」的速查表。
- **运行时衔接**：进入 [u9-l1 Driver] 与 [u9-l2 启动器]，看 `HIPDriver`/`HIPLauncher` 如何把 hsaco 加载到设备并启动 kernel，补全「编译产物 → 执行」的最后一段。
