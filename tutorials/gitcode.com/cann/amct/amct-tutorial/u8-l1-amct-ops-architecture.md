# amct_ops 架构与构建打包

## 1. 本讲目标

本讲把视线从纯 Python 的量化流程（`amct_pytorch`）移到它背后那层「贴近硬件」的 NPU 自定义算子 `amct_ops`。学完后你应该能够：

1. 说清 `amct_ops` 与 `amct_pytorch` 各自的职责边界，并能解释为什么 `amct_ops` 要做成一个**可独立安装的 wheel**，而不是直接打进 `amct_pytorch`。
2. 读懂统一构建脚本 `ops_build.sh` 的四步流水线，掌握 `--soc` 的三个平台取值（`ascend910b` / `ascend910_93` / `ascend950`）、默认平台以及它们到 NPU 架构（`dav-2201` / `dav-3510`）的映射。
3. 理解 `setup.py` 如何把「各算子的 Python 包 + 编译出的 `.so`」汇集到 `staging/` 再打成平台相关 wheel。
4. 区分算子的两种调用接口：模块导入 `from amct_ops.<op> import ...` 与 `torch.ops.amct.<op>(...)`，并理解它们共享的 `amct` 命名空间。

## 2. 前置知识

本讲是 **advanced** 阶段的第一篇 NPU 算子向讲义，假设你已经读过：

- **u1-l2**：知道 AMCT 的运行依赖分「系统级（bash/GCC/CMake/Python）」与「昇腾级（CANN 驱动/固件/Toolkit/Ops）」两层，理解 `build.sh` 与 `setup.py` 的基本分工。
- **u1-l3**：知道仓库顶层分为 `amct_pytorch`（核心量化源码，Python）与 `amct_ops`（昇腾 NPU 自定义算子，Ascend C kernel），二者职责分离。

下面几个术语本讲会反复用到，先建立直觉：

| 术语 | 通俗解释 |
|------|----------|
| **NPU / 昇腾 / Ascend** | 华为的神经网络处理器及其软件栈（CANN）。本讲里「NPU」即指昇腾。 |
| **自定义算子（custom op）** | PyTorch / torch_npu 还没内置、需要自己用底层语言实现并注册进 PyTorch 调度器的算子。 |
| **Ascend C kernel** | 跑在昇腾 AI Core 上的 C++ 核函数，是算子的「真正干活的机器码」来源。 |
| **C++ binding / extension** | 把 kernel 包一层、注册到 PyTorch 的 C++ 胶水代码（`TORCH_LIBRARY`）。 |
| **wheel（.whl）** | Python 的二进制分发包，可被 `pip install` 直接安装；与 `.tar.gz`（源码包）相对。 |
| **SOC / NPU_ARCH** | SOC 是具体的芯片型号（如 `ascend910b`）；NPU_ARCH 是编译 kernel 时用的指令集架构代号（如 `dav-2201`）。 |
| **PrivateUse1** | PyTorch 为「自定义后端」预留的后端槽位，torch_npu 占用它把 NPU 注册为一张「设备」。 |

一句话定位：`amct_pytorch` 负责「**做什么**」（量化算法与流程编排），`amct_ops` 负责「**怎么做才快**」（把 PyTorch/torch_npu 还没有的低比特算子用 Ascend C 实现并暴露给上层调用）。

## 3. 本讲源码地图

本讲涉及的关键文件，按「说明文档 → 构建脚本 → 打包配置 → 运行时入口」排列：

| 文件 | 作用 |
|------|------|
| [amct_ops/README.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md) | `amct_ops` 的「定位、分工表、构建命令、使用方式、新增算子规范」总说明书，是本讲的主线。 |
| [amct_ops/ops_build.sh](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh) | 统一构建入口：加载 CANN → 编译各算子 → 汇集到 `staging/` → 打 wheel。 |
| [amct_ops/setup.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/setup.py) | wheel 打包配置：声明平台相关（含 `.so`）、从 `staging/` 收集包与数据文件。 |
| [amct_ops/ops_init.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_init.py) | 打包时被复制成 `amct_ops/__init__.py`，提供包级文档与子模块导出。 |
| [amct_ops/hifloat8_cast/python/hifloat8_cast/\_\_init\_\_.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/python/hifloat8_cast/__init__.py) | 单算子的 Python 入口：加载 `.so` 并导出两个函数，演示「模块导入」接口。 |
| [amct_ops/hifloat8_cast/op_extension/register.cpp](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp) | C++ 注册侧：用 `TORCH_LIBRARY` 把算子注册进 `amct` 命名空间，演示「`torch.ops.amct`」接口的来源。 |
| [amct_pytorch/common/utils/quant_util.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/quant_util.py) | `amct_pytorch` 侧的调用方：HiFloat8 伪量化时**优先用 torch_npu 原生、失败回退 amct_ops**，是理解「为何独立 wheel」的关键证据。 |

真实的 `amct_ops/` 目录结构（构建产物 `build/`、`dist/`、`staging/` 除外）：

```
amct_ops/
├── README.md / README_en.md     # 总说明书
├── ops_build.sh                 # 统一构建入口
├── setup.py                     # wheel 打包配置
├── ops_init.py                  # 打包时复制为 __init__.py
├── CMakeLists.txt               # svd_quant 走的「open project」CMake 顶层
├── cmake/                       # CANN 提供的 cmake 辅助脚本
├── hifloat8_cast/               # 通用算子：FP16/BF16 ↔ HiFloat8（多平台）
│   ├── op_kernel/               #   Ascend C kernel + tiling
│   ├── op_extension/            #   C++ binding + TORCH_LIBRARY 注册
│   ├── python/hifloat8_cast/    #   Python 接口
│   └── CMakeLists.txt
└── svd_quant/                   # 研究型算子：混合 MxFp4/BF16 的 SVD 量化（仅 A5）
    ├── op_host/                 #   host 侧 def + tiling
    ├── op_kernel/               #   device 侧 kernel
    └── python/                  #   独立 setup.py + 调用脚本
```

注意两个算子的目录结构并不完全相同（`hifloat8_cast` 是 `op_kernel + op_extension`，`svd_quant` 是 `op_host + op_kernel`），这对应两条不同的构建路径，本讲 4.3 会讲透。

---

## 4. 核心概念与源码讲解

### 4.1 amct_ops 的定位与职责分工

#### 4.1.1 概念说明

随着量化精度越做越低（INT4、MXFP4、HiFloat8），PyTorch 和 torch_npu 不可能及时内置每一种新数据类型的硬件算子。`amct_ops` 就是 AMCT 为补这块空白而设的**NPU 自定义算子层**：它用 Ascend C kernel 把「低比特量化、数据类型转换」等硬件级算子实现出来，再用 C++ extension 注册进 PyTorch，最后包成 Python 可调用的接口。

README 给出的定位只有两句，但很关键：

> - `amct_ops` 是 AMCT 的 NPU 自定义算子层，负责承载 PyTorch / torch_npu 尚未覆盖的低比特量化、数据类型转换等硬件级算子。
> - 与 `amct_pytorch/` 聚焦的量化算法、压缩流程编排不同，`amct_ops` 更贴近底层硬件实现。

参见 [amct_ops/README.md:L5-L7](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L5-L7) ——这段话点明了 `amct_ops` 存在的理由（填补上游空白）与它的层次（贴近硬件）。

#### 4.1.2 核心流程

`amct_ops` 与 `amct_pytorch` 不是平级的两个模块，而是**可选的「能力补给」关系**：

```text
                 ┌───────────────────────────┐
   用户脚本 ───▶ │ amct_pytorch（Python 流程）│ ── 量化算法/编排
                 └─────────────┬─────────────┘
                               │ 运行时按需调用
                               ▼
            ┌──────────────────────────────────────┐
            │ 优先：torch_npu 原生算子（若已支持）  │
            └──────────────┬───────────────────────┘
                           │ 不支持才回退
                           ▼
                 ┌─────────────────────┐
                 │ amct_ops 自定义算子  │ ── Ascend C kernel
                 └─────────────────────┘
```

也就是说，`amct_pytorch` 在运行时**先探测** torch_npu 是否原生支持某个算子（比如 HiFloat8 cast），支持就用原生的；不支持才 `import amct_ops` 回退。这个「探测 → 回退」的决策点正是理解「为何把 `amct_ops` 做成独立 wheel」的核心，4.1.3 精读。

#### 4.1.3 源码精读

**① 分工对照表**——README 用一张表说清两者的边界：

[amct_ops/README.md:L14-L22](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L14-L22) 说明：`amct_pytorch` 关注「压缩算法与流程编排」、用 Python、产 `.tar.gz` 源码包；`amct_ops` 关注「算子底层实现」、用「Ascend C kernel & C++ binding & Python 接口」、产 **wheel 包（含 `.so`）**、且「不强依赖 AMCT 主流程」。最后这一列（复用性）是独立的根本理由。

**② 关键证据：`amct_pytorch` 把 `amct_ops` 当作可选回退**——以 HiFloat8 伪量化为例。`quant_util.py` 先用一个轻量探针判断当前 torch_npu 是否真的能做 HiFloat8 cast：

[amct_pytorch/common/utils/quant_util.py:L41-L63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/quant_util.py#L41-L63) ——`hifloat8_supported()` 不只看 `hasattr`，而是真的跑一次最小 cast 往返（liveness probe），任何异常都视为「不支持」。这是因为有些 torch_npu 构建暴露了 dtype 枚举和接口，但底层 CANN cast 未实现，真调会抛 `RuntimeError`。

随后 `hifloat8_fake_quant` 按「原生优先、amct_ops 兜底」决策：

[amct_pytorch/common/utils/quant_util.py:L66-L90](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/quant_util.py#L66-L90) ——第 75–81 行走原生 `torch_npu.npu_dtype_cast`；第 82–90 行才 `from amct_ops.hifloat8_cast import ...`，且这个 import 被包在 `try/except ImportError` 里，**找不到就抛一条指引安装 `amct_ops` 的清晰错误**，而不是让 `amct_pytorch` 一启动就崩。

这就是「独立 wheel」的工程动因：

- 如果把 `amct_ops` 打进 `amct_pytorch`，那么**每一个** `amct_pytorch` 用户在安装时都必须备齐 CANN 工具链与 Ascend C 编译器才能编译 `.so`，哪怕他用的精度根本不需要自定义算子（或 torch_npu 已原生支持）。
- 拆成独立 wheel 后，`amct_pytorch` 保持纯 Python、即装即用；只有真正需要低比特自定义算子、且 torch_npu 又不支持的用户，才额外装 `amct_ops`。
- 同时 `amct_ops` 作为「独立的 PyTorch 扩展」，也可以脱离 AMCT 流程被别的项目直接 `pip install` 使用。

> 小贴士：这种「核心包轻量 + 能力包可选」的拆分，和 `amct_pytorch` 把重的 `graph_based`（依赖 onnx/protobuf）做成懒加载（见 u1-l3）是同一种设计哲学——**让最小依赖路径尽量轻**。

#### 4.1.4 代码实践

> **实践目标**：用自己的话讲清「为什么 `amct_ops` 是独立 wheel」，并找到代码里的证据。

**操作步骤**（源码阅读型实践，无需 NPU 环境）：

1. 打开 [amct_ops/README.md:L9-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L9-L25)，阅读「独立优势」「与 amct_pytorch 的分工」「使用方式」三节。
2. 打开 [amct_pytorch/common/utils/quant_util.py:L75-L90](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/quant_util.py#L75-L90)，观察「原生优先、amct_ops 回退」的 try/except 结构。
3. 回答两个问题（写下来）：
   - 假如某天 torch_npu 原生支持了 HiFloat8 cast，`amct_ops` 还需要安装吗？为什么？
   - 为什么回退的 `import amct_ops...` 必须放在 `try/except ImportError` 里，而不是文件顶部？

**需要观察的现象 / 预期结果**：

- 第 1 问：不需要。`hifloat8_supported()` 会返回 `True`，走第 75–81 行原生分支，`amct_ops` 永远不会被 import。
- 第 2 问：若放顶部，则没装 `amct_ops` 的用户一启动 `amct_pytorch` 就 ImportError 崩溃；放 try/except 里则只在「真的需要、又确实没装」时才报错，且报错信息自带安装指引。

> 待本地验证：以上结论来自静态阅读；若你手头有 NPU 环境，可分别用「装了 amct_ops」与「卸载 amct_ops 且 torch_npu 不支持 hifloat8」两种情况触发，观察报错文案是否与源码一致。

#### 4.1.5 小练习与答案

**练习 1**：README 分工表里 `amct_pytorch` 的产物是 `.tar.gz`、`amct_ops` 的产物是 wheel。为什么两者的打包格式不同？

> **参考答案**：`.tar.gz` 是源码包（`amct_pytorch` 是纯 Python，装上就能跑，不需要预编译）；wheel 是二进制包（`amct_ops` 含针对特定 SOC 编译的 `.so`，打成 wheel 才能让用户 `pip install` 后直接拿到对应平台的二进制，免去用户本机编译 kernel）。

**练习 2**：`hifloat8_supported()` 为什么要「真跑一次最小 cast」而不是只 `hasattr(torch_npu, 'hifloat8')`？

> **参考答案**：有些 torch_npu 构建会暴露 dtype 枚举与接口符号，但底层 CANN 的 cast 实现尚未到位，真正调用会抛 `RuntimeError`（aclnn 错误 161002）。只用 `hasattr` 会误判为「支持」，导致后续量化在运行中才崩溃；liveness probe 把这种「假支持」也判为不支持，从而正确回退到 `amct_ops`。

---

### 4.2 两种调用接口与 amct 命名空间

#### 4.2.1 概念说明

`amct_ops` 的每个算子都同时暴露**两种**调用接口：

1. **模块导入**：`from amct_ops.hifloat8_cast import encode_to_hifloat8` —— 有 IDE 补全、有文档字符串，写业务代码时首选。
2. **`torch.ops.amct`**：`torch.ops.amct.encode_to_hifloat8(x)` —— 与 torch_npu 上游算子（如 `torch.ops.npu.xxx`）风格一致，适合在算子调度、图模式等场景统一处理。

两种接口背后其实是**同一个 C++ 实现**：模块导入只是 `torch.ops.amct.<op>` 的一个 Python 包装。而所有算子都注册在同一个 `amct` 命名空间下——这是 README 强制的命名约束。

参见 [amct_ops/README.md:L23-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L23-L25) 与 [amct_ops/README.md:L90-L106](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L90-L106) 的使用示例。

#### 4.2.2 核心流程

一次 `encode_to_hifloat8(x)` 调用的来龙去脉：

```text
# Python 侧
import amct_ops.hifloat8_cast          # 触发包 __init__.py
        │
        ▼
torch.ops.load_library(".../libhifloat8_cast_ops.so")   # 加载编译好的 .so
        │
        ▼  （.so 加载时执行其中的 TORCH_LIBRARY 注册）
# C++ 侧（register.cpp）
TORCH_LIBRARY_FRAGMENT(amct, m) { m.def("encode_to_hifloat8(...)"); }
TORCH_LIBRARY_IMPL(amct, PrivateUse1, m) { m.impl(...); }   # NPU 实现
TORCH_LIBRARY_IMPL(amct, Meta, m) { m.impl(...); }          # 形状推导
        │
        ▼  （至此 torch.ops.amct.encode_to_hifloat8 可用）
# 两种入口都指向同一个注册函数
torch.ops.amct.encode_to_hifloat8(x)   ←── 方式二
encode_to_hifloat8(x)                  ←── 方式一（Python 薄包装）
```

两个关键点：

- **`PrivateUse1`** 是 torch_npu 占用的后端槽位，`TORCH_LIBRARY_IMPL(amct, PrivateUse1, m)` 表示「这个算子在 NPU 上的实现」。
- **`Meta`** 实现只做形状/类型推导、不真正算，供 `torch.compile` / 图模式做符号化推导用；没有它，图模式下会报「no Meta kernel」。

#### 4.2.3 源码精读

**① Python 入口加载 `.so`**——单算子的 `__init__.py` 在被 import 时主动加载编译产物：

[amct_ops/hifloat8_cast/python/hifloat8_cast/\_\_init\_\_.py:L44-L49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/python/hifloat8_cast/__init__.py#L44-L49) ——用 `__file__` 定位同目录下的 `libhifloat8_cast_ops.so`，`torch.ops.load_library` 加载它；随后 `from .ops import encode_to_hifloat8, decode_from_hifloat8` 把 `torch.ops.amct.*` 再包一层导出。注意第一行 `import torch_npu`（带 `noqa`）——它必须先执行，以把 `PrivateUse1` 后端注册成 NPU，否则 `.so` 里的 NPU 实现无处挂载。

**② C++ 侧注册到 `amct` 命名空间**——schema 与实现分离：

[amct_ops/hifloat8_cast/op_extension/register.cpp:L24-L27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp#L24-L27) ——`TORCH_LIBRARY_FRAGMENT(amct, m)` 用 `m.def(...)` 声明算子签名（`FRAGMENT` 表示「往已存在的 `amct` 命名空间追加」，多个算子各自追加不会冲突）。

[amct_ops/hifloat8_cast/op_extension/register.cpp:L46-L49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp#L46-L49) ——`TORCH_LIBRARY_IMPL(amct, PrivateUse1, m)` 把签名绑定到 NPU 实现 `EncodeImpl/DecodeImpl`。同一个文件后面还有 `TORCH_LIBRARY_IMPL(amct, Meta, m)` 提供 Meta 后端实现（形状推导）。

**③ 命名空间唯一性约束**——README 明确要求所有算子注册到 `amct`：

[amct_ops/README.md:L145-L151](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L145-L151) ——「所有算子必须注册到 `amct` 命名空间」，与包名 `amct_ops` 一致，便于调用方区分 AMCT 自定义算子与 `torch_npu` 上游算子；算子名在 `amct` 内须唯一，新增前先检索 `torch.ops.amct` 是否已有同名算子。

#### 4.2.4 代码实践

> **实践目标**：用 Python 内省看清「两种接口、同一实现」。

**操作步骤**（源码阅读型，装好 amct_ops 后可实跑）：

1. 阅读 [amct_ops/README.md:L108-L116](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L108-L116) 的「Python 内省」小节。
2. 在装好 amct_ops 的环境里执行（**待本地验证**）：

   ```python
   import amct_ops
   help(amct_ops)                                   # 查看所有子模块及接口列表
   import amct_ops.hifloat8_cast
   help(amct_ops.hifloat8_cast.encode_to_hifloat8)  # 单函数签名 + 文档
   ```

3. 再对比两种调用是否等价（**待本地验证**，需 NPU）：

   ```python
   import torch
   x = torch.randn(1024, dtype=torch.float16, device="npu")
   import amct_ops.hifloat8_cast                       # 触发 .so 加载
   a = amct_ops.hifloat8_cast.encode_to_hifloat8(x)    # 方式一
   b = torch.ops.amct.encode_to_hifloat8(x)            # 方式二
   ```

**需要观察的现象 / 预期结果**：

- `help(amct_ops)` 列出 `hifloat8_cast` 子模块及其 `encode_to_hifloat8 / decode_from_hifloat8`。
- 两种调用产出的 `a` 与 `b` 是同形状、同 dtype（`uint8`）的同结果张量，因为它们绑定到同一个 C++ 实现。

#### 4.2.5 小练习与答案

**练习 1**：为什么算子 `__init__.py` 里要先 `import torch_npu` 再 `torch.ops.load_library`？

> **参考答案**：`torch_npu` 的导入会把 `PrivateUse1` 后端注册成 NPU 设备。若先 `load_library`，`.so` 里的 `TORCH_LIBRARY_IMPL(amct, PrivateUse1, ...)` 实现就找不到对应后端可挂载，算子调用会失败。

**练习 2**：`TORCH_LIBRARY_FRAGMENT(amct, m)` 里的 `FRAGMENT` 有什么用？如果两个算子都用它注册会不会互相覆盖？

> **参考答案**：`FRAGMENT` 表示「向一个已存在的命名空间追加定义」，而非「定义一个新命名空间」。多个算子各自用 `FRAGMENT` 往 `amct` 里 `m.def` 不同算子名，是追加而非覆盖，因此不会互相冲突——这正是「多算子共存于同一 `amct` 命名空间」的基础。

---

### 4.3 ops_build.sh 四步构建流水线

#### 4.3.1 概念说明

`amct_ops` 下的所有算子，通过根目录**一个**脚本 `ops_build.sh` 一次性编译并打包成 wheel。它的核心参数只有两个：

- `--soc <soc>`：目标芯片平台，可选 `ascend910b` / `ascend910_93` / `ascend950`，**默认 `ascend910b`**。
- `<op>`：只构建指定算子，省略则构建全部。

参见脚本头部的用法说明 [amct_ops/ops_build.sh:L20-L35](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L20-L35)。

#### 4.3.2 核心流程

脚本用 `echo "[1/4] ... [2/4] ... [3/4] ... [4/4] ..."` 把流程显式切成四步：

```text
[1/4] 加载 CANN 环境   ← 必须有 ASCEND_HOME_PATH，source set_env.sh
        │
[2/4] 编译各算子         ← build_op()：按算子类型走两条不同 CMake 路径
        │                   ── SOC → NPU_ARCH 映射（dav-2201 / dav-3510）
        ▼
[3/4] 汇集到 staging/    ← collect_op()：把各算子 python/<pkg>/ 与 build/*.so
        │                   拷到 staging/amct_ops/<pkg>/，并把 ops_init.py
        │                   复制成 staging/amct_ops/__init__.py
        ▼
[4/4] 构建 wheel         ← pip wheel . -w dist/ --no-deps --no-build-isolation
                            产物：dist/amct_ops-1.0.0-cp*-cp*-linux_<arch>.whl
```

**SOC → NPU_ARCH 映射**是编译 kernel 的关键：kernel 要按芯片指令集架构编译，而 SOC 型号与架构代号不是一一对应：

[amct_ops/ops_build.sh:L90-L100](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L90-L100) ——`ascend910b` 与 `ascend910_93` **共用 `dav-2201`**（ISA 相同，UB 大小由运行时平台 API 区分），`ascend950` 用 `dav-3510`。三个 SOC 平台与默认值的关系如下表：

| `--soc` 取值 | 对应芯片 | NPU_ARCH | 说明 |
|--------------|----------|----------|------|
| `ascend910b`（**默认**） | Ascend A2（910B1/B2/B3） | `dav-2201` | 注释里标注为 A2 |
| `ascend910_93` | Ascend A3（910_93 / 910B4） | `dav-2201` | 与 A2 同 ISA |
| `ascend950` | Ascend A5（950） | `dav-3510` | 需 CANN 编译器支持 dav-3510 |

> 直觉解释：为什么 A2 和 A3 共用一个架构代号？因为它们属同一代指令集（ISA），同一份 kernel 二进制可在两者上跑；区别只在运行时的硬件参数（如 UB 内存大小），这些由运行时平台 API 查询，不需要编译期区分。A5 是新一代，指令集不同，所以单独用 `dav-3510`。

#### 4.3.3 源码精读

**① 默认 SOC 与参数解析**：

[amct_ops/ops_build.sh:L60-L88](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L60-L88) ——第 61 行 `SOC="ascend910b"` 即默认平台；解析循环支持 `--soc <值>`、`--soc=<值>` 两种写法，遇到非法选项或 `--soc` 缺参数都会打印用法并 `exit 1`。注意它**没有** `--help` 分支——这是本讲实践任务要注意的点（见 4.3.4）。

**② 第 1 步：加载 CANN 环境**：

[amct_ops/ops_build.sh:L102-L113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L102-L113) ——强制要求环境变量 `ASCEND_HOME_PATH` 已设置且其下 `set_env.sh` 存在，否则报错退出；通过后 `source` 它把 CANN 的编译器、头文件、库路径注入当前 shell。这一步是「编译态」对 CANN Toolkit 的依赖（见 u1-l2）。

**③ 第 2 步：`build_op` 的两条路径**——这是本讲最值得读的一段，因为它揭示了 `amct_ops` 里其实有**两种算子构建风格**：

[amct_ops/ops_build.sh:L118-L153](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L118-L153) ——

- **`svd_quant` 特例（第 123–144 行）**：仅当 `--soc ascend950` 才构建，否则加入 `SKIP_OP_LIST` 跳过（第 124–128 行的 `'svd_quant' operator builds for ascend950 socket only`）。它走的是 CANN「open project」CMake（`-DBUILD_OPEN_PROJECT=ON`、`-DASCEND_COMPUTE_UNIT=${SOC}`、`-DCUSTOM_ASCEND_CANN_PACKAGE_PATH`），先 `cmake --build . --target package` 出 `.run` 包，再单独 `python3 <op>/python/setup.py build bdist_wheel`，最后把 `build/` 下的 `.so` 收拢重命名为 `libsvd_quant.so`。
- **通用路径（第 145–152 行，`hifloat8_cast` 走这条）**：直接 `cmake -S <op> -B <op>/build -DNPU_ARCH=... -DASCEND_ARCH_DIR=...` 再 `cmake --build`，产物是 `libhifloat8_cast_ops.so`。这条路径用 `NPU_ARCH`（`dav-2201`/`dav-3510`）而非 `ASCEND_COMPUTE_UNIT`。

  > 这正是 4.1.2 目录结构里 `hifloat8_cast`（`op_kernel + op_extension`）与 `svd_quant`（`op_host + op_kernel`）结构不同的根源——两套 CMake 模板对应两种算子组织方式。u8-l2 会精读 `hifloat8_cast` 的三层结构，u8-l3 会讲新增算子流程并以 `svd_quant` 为案例。

**④ 第 3 步：汇集到 `staging/`**：

[amct_ops/ops_build.sh:L164-L194](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L164-L194) ——先把 `ops_init.py` 复制成 `staging/amct_ops/__init__.py`（第 167 行），再对每个算子的 `python/<pkg>/` 目录：拷贝顶层 `.py`、拷贝 `build/*.so` 到 `staging/amct_ops/<pkg>/`（第 186–190 行）。`collect_op` 会跳过 `SKIP_OP_LIST` 里被跳过的算子（如非 A5 平台上的 `svd_quant`），也跳过以 `_` 或 `.` 开头的目录。

**⑤ 第 4 步：构建 wheel**：

[amct_ops/ops_build.sh:L212-L216](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L212-L216) ——`pip wheel . -w dist/ --no-deps --no-build-isolation` 在 `amct_ops/` 根目录跑，由 `setup.py` 接管（从 `staging/` 取包），最终在 `dist/` 产出 `amct_ops-1.0.0-cp*-cp*-linux_<arch>.whl`。末尾还会用 `python3 -m zipfile -l` 列出 wheel 内容，方便核对 `.so` 是否都打进去。

#### 4.3.4 代码实践

> **实践目标**：列出 `ops_build.sh` 支持的 `--soc` 平台与默认平台，并理解 `svd_quant` 的平台限制。

**操作步骤**：

1. 执行（或阅读）：

   ```bash
   cd amct_ops/
   bash ops_build.sh --help     # 注意：脚本未实现 --help 分支
   ```

   - **预期现象**：因为脚本参数解析里没有 `--help` 分支（见 [ops_build.sh:L78-L82](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L78-L82) 的 `-*` 兜底分支），传 `--help` 会被当作「未知选项」打印用法并 `exit 1`。所以正确做法是**读脚本头部注释**（[ops_build.sh:L20-L35](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L20-L35)）或读 README 的构建命令小节（[README.md:L61-L76](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L61-L76)）。这是「不能假装命令跑过」的典型例子。

2. 据此填写下表（答案已在 4.3.2 给出）：

   | `--soc` | 平台 | NPU_ARCH | 是否默认 |
   |---------|------|----------|----------|
   | ? | A2 | ? | ? |
   | ? | A3 | ? | ? |
   | ? | A5 | ? | ? |

3. 追问：若执行 `bash ops_build.sh svd_quant`（不指定 `--soc`），会发生什么？

**需要观察的现象 / 预期结果**：

- 三行答案：`ascend910b`/A2/`dav-2201`/**是默认**；`ascend910_93`/A3/`dav-2201`/否；`ascend950`/A5/`dav-3510`/否。
- 第 3 问：默认 `SOC=ascend910b`，`svd_quant` 的分支判断 `[ ${SOC} != "ascend950" ]` 成立，于是打印 `'svd_quant' operator builds for ascend950 socket only` 并把它加入 `SKIP_OP_LIST` 跳过——不会报错，但 wheel 里不会包含 `svd_quant`。

> 待本地验证：以上「`--help` 被拒」「svd_quant 被跳过」的行为来自静态阅读；在有 CANN 环境的机器上可实测确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ascend910b` 和 `ascend910_93` 共用 `dav-2201`，却还要分成两个 `--soc` 取值？

> **参考答案**：两者 ISA 相同，kernel 二进制可共用（故 `NPU_ARCH` 同为 `dav-2201`）；但运行时硬件参数（如 UB 大小）不同，需由运行时平台 API 区分。分成两个 `--soc` 让用户明确表达目标平台，也方便 `svd_quant` 这类有平台限制的算子按 SOC 做条件构建。

**练习 2**：`build_op` 里 `svd_quant` 和通用算子分别用什么 CMake 变量指定目标平台？

> **参考答案**：`svd_quant` 用 `ASCEND_COMPUTE_UNIT=${SOC}`（open project 风格，值是 SOC 型号）；通用算子（`hifloat8_cast`）用 `NPU_ARCH`（值是 `dav-2201`/`dav-3510` 架构代号）。两者源自不同的 CMake 模板。

---

### 4.4 setup.py wheel 打包与 staging 汇集

#### 4.4.1 概念说明

`setup.py` 的工作很简单：把 `ops_build.sh` 准备好的 `staging/` 目录「装进」一个 wheel。但它有两个不寻常之处：

1. **平台相关 wheel**：因为含编译好的 `.so`，必须声明成「平台相关」，否则 `pip` 会把它当成纯 Python 包。
2. **`staging/` 作为包根**：`setup.py` 不直接从各算子目录收包，而是统一从 `staging/` 收——构建逻辑（`ops_build.sh`）与打包声明（`setup.py`）因此解耦。

参见 [amct_ops/setup.py:L19-L28](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/setup.py#L19-L28) 的模块文档字符串，它明确写道：「构建脚本在调用前，会先将各算子的 Python 包和编译产物（`.so`）统一汇集到 `staging/` 目录，因此这里使用标准的 `find_packages()` 即可。」

#### 4.4.2 核心流程

```text
staging/                         ← ops_build.sh 第 3 步产出
└── amct_ops/
    ├── __init__.py              ← 由 ops_init.py 复制而来
    └── hifloat8_cast/
        ├── __init__.py          ← 算子 Python 接口
        ├── ops.py
        └── libhifloat8_cast_ops.so   ← 编译产物
                │
                ▼  setup.py
        find_packages(where='staging')          → 发现 amct_ops、amct_ops.hifloat8_cast
        package_data[amct_ops.hifloat8_cast]    → ['libhifloat8_cast_ops.so']
        distclass=BinaryDistribution            → 强制平台相关
                │
                ▼  pip wheel .
        dist/amct_ops-1.0.0-cp*-cp*-linux_<arch>.whl
```

#### 4.4.3 源码精读

**① 强制平台相关 wheel**：

[amct_ops/setup.py:L35-L39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/setup.py#L35-L39) ——自定义 `BinaryDistribution`，覆写 `has_ext_modules()` 恒返回 `True`。setuptools 据此判定这个包含扩展模块（`.so`），从而生成带平台标签（`linux_x86_64` / `linux_aarch64`）与 Python 标签（`cp311-cp311` 等）的 wheel，而不是「万能」的纯 Python wheel。

**② 收集 `.so` 为 `package_data`**：

[amct_ops/setup.py:L42-L54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/setup.py#L42-L54) ——扫描 `staging/amct_ops/<sub>/` 下每个子目录，把 `.so` 文件登记进 `package_data[f'amct_ops.{sub}']`。这样 wheel 才会把 `.so` 一起打包；否则默认只收 `.py`，装出来的包里没有 `.so`，`torch.ops.load_library` 会找不到库。

**③ 主 `setup()` 调用**：

[amct_ops/setup.py:L56-L73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/setup.py#L56-L73) ——关键字段：`name="amct_ops"`、`version="1.0.0"`、`packages=find_packages(where='staging')`（从 `staging/` 发现包）、`package_dir={'': 'staging'}`（告诉 setuptools 包根是 `staging/` 而非当前目录）、`python_requires=">=3.9"`、`install_requires=["torch", "torch_npu"]`、`distclass=BinaryDistribution`。

**④ `ops_init.py` 变身 `__init__.py`**：

[amct_ops/ops_init.py:L18-L48](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_init.py#L18-L48) ——这个文件本身**不是**包的 `__init__.py`（它放在 `amct_ops/` 根目录而非 `amct_ops/amct_ops/`），而是被 `ops_build.sh` 第 167 行复制成 `staging/amct_ops/__init__.py`。它的文档字符串成为 `help(amct_ops)` 看到的包说明（列出可用子模块与函数），`__all__ = ['hifloat8_cast']` 与 `from . import hifloat8_cast` 让「`import amct_ops`」即可触达子模块。这种「源文件叫 `ops_init.py`、打包时改名为 `__init__.py`」的写法，避免了在仓库里出现 `amct_ops/amct_ops/__init__.py` 这种别扭的嵌套路径。

#### 4.4.4 代码实践

> **实践目标**：不装 wheel，直接检查它的内部结构，验证 `.so` 与 `__init__.py` 都在。

**操作步骤**（源码阅读型，任何机器可做）：

1. 阅读 [amct_ops/README.md:L78-L88](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L78-L88) 关于构建产物与安装的说明。
2. 若你已跑过 `bash ops_build.sh`，执行（**待本地验证**，需先有产物）：

   ```bash
   cd amct_ops/
   python3 -m zipfile -l dist/amct_ops-*.whl | grep -v "dist-info"
   ```

**需要观察的现象 / 预期结果**：

- 列表里应能看到 `amct_ops/__init__.py`（即 `ops_init.py` 的化身）、`amct_ops/hifloat8_cast/__init__.py`、`amct_ops/hifloat8_cast/ops.py`，以及关键的 `amct_ops/hifloat8_cast/libhifloat8_cast_ops.so`。
- 文件名形如 `amct_ops-1.0.0-cp311-cp311-linux_x86_64.whl`：`1.0.0` 来自 `setup.py` 的 `version`；两个 `cp311` 是 Python 实现/ABI 标签；`linux_x86_64`（或 `linux_aarch64`）是构建主机架构，由 `BinaryDistribution` 触发生成。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `setup.py` 里收集 `package_data` 的那段循环（第 45–54 行），构建还能成功吗？装出来的包会有什么问题？

> **参考答案**：构建（`pip wheel`）仍能成功，因为 `.so` 物理上在 `staging/` 里；但 wheel 不会把 `.so` 打进去（setuptools 默认只收 `.py`）。用户 `pip install` 后 `import amct_ops.hifloat8_cast` 时 `torch.ops.load_library` 找不到 `libhifloat8_cast_ops.so` 而报错。

**练习 2**：为什么要把 `staging/` 作为 `package_dir` 的根，而不是让 `setup.py` 直接去各算子目录收包？

> **参考答案**：为了把「构建（编译 `.so`、决定打哪些算子）」与「打包声明」解耦。`ops_build.sh` 负责把要发的包按统一布局铺到 `staging/`（包括跳过非 A5 的 `svd_quant`、把 `ops_init.py` 改名），`setup.py` 只需对 `staging/` 做一次「标准」的 `find_packages`，不必认识每个算子的目录细节。新增算子时也因此无需改 `setup.py`。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**画出 amct_ops 的完整生命周期**」的小任务。

**任务**：用一张图（文字版流程图即可）把以下三件事连成一条线，并配上源码行号证据：

1. **为何独立**：从 [amct_pytorch/common/utils/quant_util.py:L75-L90](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/utils/quant_util.py#L75-L90) 的「原生优先、amct_ops 回退」出发，说明 `amct_ops` 是「可选能力包」，并解释因此它必须是独立 wheel。

2. **如何构建**：从 `bash ops_build.sh --soc ascend910_93` 出发，标出四步流水线（[ops_build.sh:L102-L113](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L102-L113) 加载 CANN → [L118-L153](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L118-L153) 编译 → [L164-L194](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L164-L194) 汇集 → [L212-L216](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L212-L216) 打 wheel），并在编译步骤旁标注 `ascend910_93 → dav-2201`（[L90-L100](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L90-L100)）。

3. **如何被调用**：从 `import amct_ops.hifloat8_cast` 出发，画出 `load_library(.so)`（[\_\_init\_\_.py:L44-L47](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/python/hifloat8_cast/__init__.py#L44-L47)）→ `TORCH_LIBRARY` 注册 `amct` 命名空间（[register.cpp:L24-L49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp#L24-L49)）→ 两种接口等价可达。

**验收标准**：

- 图中能体现「`amct_pytorch` 只在 torch_npu 不支持时才依赖 `amct_ops`」这一可选关系。
- 能解释「为什么 A2 与 A3 共用 `dav-2201`、A5 用 `dav-3510`」。
- 能指出 `svd_quant` 在非 A5 平台会被静默跳过（进 `SKIP_OP_LIST`），所以默认 wheel 通常只含 `hifloat8_cast`。

> 待本地验证：若有 CANN 环境，可分别用 `--soc ascend910b` 与 `--soc ascend950` 跑一次构建，对比 `dist/` 产物与 `python3 -m zipfile -l` 列出的内容差异（重点看 `svd_quant` 是否出现）。

## 6. 本讲小结

- **定位**：`amct_ops` 是 AMCT 的 NPU 自定义算子层，用 Ascend C kernel 补齐 PyTorch/torch_npu 尚未覆盖的低比特量化与数据类型转换算子；`amct_pytorch` 只在 torch_npu 原生不支持时才回退调用它（`quant_util.py` 的「原生优先、amct_ops 兜底」）。
- **为何独立 wheel**：让 `amct_pytorch` 保持纯 Python、即装即用；只有真正需要自定义算子且 torch_npu 不支持的用户才需额外安装 `amct_ops`，免去所有人被迫配齐 CANN/Ascend C 编译器。
- **统一构建**：`ops_build.sh` 四步流水线（加载 CANN → 编译各算子 → 汇集 `staging/` → `pip wheel`），`--soc` 三选一（`ascend910b` 默认 / `ascend910_93` / `ascend950`），映射到 `dav-2201`（A2、A3 共用）与 `dav-3510`（A5）。
- **两种构建风格**：通用算子（`hifloat8_cast`）走 `NPU_ARCH` 的简捷 CMake 路径；`svd_quant` 走 CANN open project 路径且**仅 A5** 构建，非 A5 被静默跳过。
- **打包**：`setup.py` 以 `staging/` 为包根，用 `BinaryDistribution` 强制平台相关 wheel，用 `package_data` 把 `.so` 一起打入；`ops_init.py` 在打包时被复制成 `amct_ops/__init__.py`。
- **两种接口 + 一个命名空间**：所有算子注册到 `amct` 命名空间（`TORCH_LIBRARY_FRAGMENT(amct, m)` + `PrivateUse1`/`Meta` 实现），同时暴露 `from amct_ops.<op> import ...` 与 `torch.ops.amct.<op>(...)` 两种等价接口。

## 7. 下一步学习建议

本讲只讲了 `amct_ops` 的「架构与构建打包」，没有深入任何一个算子的内部实现。建议按以下顺序继续：

1. **u8-l2 hifloat8_cast 算子实现剖析**：以 `hifloat8_cast` 为样本，精读「`op_kernel`（Ascend C kernel + tiling）→ `op_extension`（C++ binding + `TORCH_LIBRARY`）→ `python`（接口包装）」三层结构，理解本讲提到的 `TORCH_LIBRARY_FRAGMENT` / `PrivateUse1` / `Meta` 到底怎么写。
2. **u8-l3 新增 NPU 算子的开发流程**：以 `svd_quant` 为案例，学习「新增一个算子目录、注册到 `amct` 命名空间、被 `ops_build.sh` 自动发现打包」的完整流程，并理解 `op_host`（tiling）+ `op_kernel` 这套 open project 组织方式。
3. **横向对照**：回头读 u7-l2（量化数据类型与 export_deploy 落盘），对比 `amct_pytorch` 内的纯 Python 伪量化（`fake_quant`）与 `amct_ops` 的真硬件算子（如 `hifloat8_cast`）在 HiFloat8 这条链路上如何分工——前者管训练态误差模拟，后者管部署态硬件执行。
