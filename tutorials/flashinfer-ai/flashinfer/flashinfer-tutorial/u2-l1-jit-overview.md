# JIT 编译概览：三层架构与开发循环

## 1. 本讲目标

上一讲（u1-l5）你已经亲手调用了 `single_decode_with_kv_cache`，并观察到「第一次调用很慢、第二次很快」——这正是 JIT（Just-In-Time，即时编译）在工作。本讲我们要把这一次性现象拆开来看清楚：

- 理解 **FlashInfer 为什么默认走 JIT**，它解决了什么工程问题、付出了什么代价；
- 掌握贯穿全项目的 **三层 JIT 架构**（`JitSpec` 定义层 → 代码生成层 → 编译加载层）的职责划分；
- 看懂 **开发循环（edit .cuh → 自动重编译）** 背后的机制，知道为什么改了头文件不用重装包就能生效。

学完本讲，你应该能在源码里指出「一条 Python API 调用最终是怎么变成一个 `.so` 文件并被加载执行的」，并能定位自己机器上的 JIT 工作区。本讲是第 2 单元其余四讲（JitSpec/env、代码生成模式、编译上下文、缓存失效）的总纲，后续每一讲都在本讲的三层框架里展开某一个局部。

## 2. 前置知识

本讲假设你已经掌握 u1 系列的内容：知道 FlashInfer 是面向 LLM 推理的 GPU kernel 库（u1-l1）、能从源码 editable 安装（u1-l2）、看懂 `include/`（框架无关 kernel 模板）、`csrc/`（TVM-FFI 绑定层）、`flashinfer/`（Python 包）三层目录分工（u1-l3）、并跑通过一次 `single_decode`（u1-l5）。

下面补充几个本讲会反复用到、但前面没细讲的术语：

- **JIT（Just-In-Time）vs AOT（Ahead-Of-Time）**：AOT 指在打包发版时就把所有 kernel 编译成二进制（`.so`/`.cubin`）；JIT 指在用户运行时，按实际需要的参数组合现场生成代码并编译。FlashInfer **默认 JIT，AOT 作为可选加速包**（`flashinfer-jit-cache` / `flashinfer-cubin`）。
- **nvcc**：NVIDIA 的 CUDA C++ 编译器，把 `.cu` 源码编译成 GPU 可执行的机器码（`.o`），再链接成动态库 `.so`。它是编译耗时的主要来源。
- **ninja**：一个轻量级构建工具，FlashInfer 用它代替 CMake 来驱动「`.cu` → `.o` → `.so`」。ninja 的两个关键能力是 **增量编译**（只重编改动的文件）和 **依赖追踪**（通过 depfile 知道一个 `.cu` 依赖哪些头文件）。
- **Jinja**：Python 的模板引擎，FlashInfer 用它把「带占位符的 C++ 模板」渲染成「具体类型的 C++ 代码」。
- **TVM-FFI**：跨语言的函数调用接口（u1-l3 已介绍）。编译出的 `.so` 通过 `tvm_ffi.load_module` 加载回 Python，它导出的每个函数都用 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 标记。
- **workspace / 工作区**：FlashInfer 在磁盘上存放「生成的源码」和「编译产物」的目录，默认在 `~/.cache/flashinfer/` 下，按「版本号/CUDA 架构」分层。

## 3. 本讲源码地图

本讲主要围绕 JIT 系统的「总入口」与「骨架」展开，涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [flashinfer/jit/core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py) | JIT 的核心：定义 `JitSpec`（编译任务的数据结构）、`gen_jit_spec`（组装编译参数）、`build`/`build_and_load`（编译并加载）、`clear_cache_dir`、全局注册表 `jit_spec_registry`。**本讲的主线源码。** |
| [flashinfer/jit/env.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py) | 定义所有工作区路径：`FLASHINFER_JIT_DIR`（编译产物）、`FLASHINFER_GEN_SRC_DIR`（生成的源码）、`FLASHINFER_INCLUDE_DIR`/`FLASHINFER_CSRC_DIR`（只读源码模板）。决定了文件写到哪里。 |
| [flashinfer/jit/cpp_ext.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py) | 第三层「编译」的实现：`generate_ninja_build_for_op` 生成 `build.ninja`，`run_ninja` 调用 ninja 真正编译。 |
| [flashinfer/jit/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/utils.py) | 工具函数：`write_if_different`（只在内容变化时写文件，避免无谓触发重编译）、`dtype_map`（torch dtype → C++ 类型）。 |
| [flashinfer/jit/activation.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py) | 第二层「代码生成」的**最简范例**：`gen_act_and_mul_module` 展示了标准的「渲染 Jinja → 写 `.cu` → 返回 `JitSpec`」流程。本讲和后续多讲都拿它当例子。 |
| [flashinfer/activation.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/activation.py) | 用户侧 Python API：`silu_and_mul` 等，`get_act_and_mul_module` 用 `@functools.cache` + `build_and_load` 把「代码生成」和「编译加载」串起来。 |
| [include/flashinfer/activation.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/activation.cuh) | 框架无关的 CUDA kernel 模板（header-only）。本讲「实时重载」实践要改的就是它的注释。 |
| [flashinfer/__main__.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py) | `flashinfer clear-cache` CLI 命令，背后调用 `clear_cache_dir`，用于实践中的对比实验。 |

> 阅读建议：本讲先把 [core.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py) 当成「目录」扫一遍类与函数名（`JitSpec`、`gen_jit_spec`、`build`、`build_and_load`、`clear_cache_dir`、`build_jit_specs`），建立整体印象，再随各模块精读对应片段。

## 4. 核心概念与源码讲解

### 4.1 为什么默认 JIT：动机与三层架构总览

#### 4.1.1 概念说明

FlashInfer 的 kernel 都是 **C++ 模板（template）**。一个注意力 kernel 会因为「输入是 fp16 还是 bf16」「head_dim 是 128 还是 256」「用 FA2 还是 FA3」等参数的不同，编译出**完全不同的机器码**。如果走 AOT，就要在发版时穷举所有参数组合预编译——以 attention 为例，dtype × head_dim × posenc × mask × backend 的组合轻松上千种，每种还要乘上 SM 架构（SM75/80/89/90/100/…），预编译产物会大到无法分发，而且 99% 的组合用户根本用不到。

JIT 的核心动机就是**把「编译什么」推迟到「知道用户真正需要什么」之后**：用户调用 `single_decode` 时传了 `dtype=torch.float16`、`head_dim=128`，FlashInfer 才现场为「这一个组合」生成并编译一份专用 kernel，编译完缓存到磁盘，下次同样的组合直接复用。

代价是**首次调用的编译延迟**（几秒到几十秒）。FlashInfer 用两级缓存（进程内 + 磁盘）把这个代价摊薄到「一次」，并用可选的 AOT 包（`flashinfer-jit-cache`）为生产环境预填缓存。工程上的收益远大于代价：

- **开发体验**：改 kernel 源码不用重装包，下次调用自动重编译（本讲的「实时重载」）；
- **分发体积**：核心包只带源码模板，不带海量二进制；
- **可扩展**：加一个新 dtype/架构只需加一个模板分支，不用维护预编译矩阵。

#### 4.1.2 核心流程

整个 JIT 系统可以看成 **三个层（Layer）依次接力**，外加一个「缓存」做加速：

```
                  ┌─────────────────────────────────────────────┐
  Python API      │ silu_and_mul(x)                             │  flashinfer/activation.py
  （用户侧）       │   └─ get_act_and_mul_module("silu")         │  @functools.cache
                  └──────────────────────┬──────────────────────┘
                                         │ 触发
                  ┌──────────────────────▼──────────────────────┐
  Layer 2 代码生成 │ gen_act_and_mul_module("silu")              │  flashinfer/jit/activation.py
                  │   渲染 Jinja → 写 silu_and_mul.cu            │  → 返回一个 JitSpec
                  └──────────────────────┬──────────────────────┘
                                         │ spec.build_and_load()
                  ┌──────────────────────▼──────────────────────┐
  Layer 1 定义     │ JitSpec(name, sources, flags, …)            │  flashinfer/jit/core.py
  （配料表）       │   gen_jit_spec(...) 把编译参数组装好         │  + jit_spec_registry 登记
                  └──────────────────────┬──────────────────────┘
                                         │ build() / load()
                  ┌──────────────────────▼──────────────────────┐
  Layer 3 编译加载 │ write_ninja → generate_ninja_build_for_op   │  core.py + cpp_ext.py
                  │   → run_ninja(nvcc 编译) → .so              │
                  │   → tvm_ffi.load_module(.so) → Python 模块  │
                  └──────────────────────┬──────────────────────┘
                                         │
                                 ┌───────▼───────┐
  缓存加速         磁盘 .so      │ ~/.cache/...  │  下次同组合直接命中，跳过 Layer 3
                                │ cached_ops/   │
                                └───────────────┘
```

三层各自的「一句话职责」：

| 层 | 职责 | 关键产物 / 入口 |
|----|------|----------------|
| **Layer 1 定义层** | 把「编译一个模块需要的一切信息」打包成一个数据结构 `JitSpec`（名字、源文件、编译选项）。 | `JitSpec`、`gen_jit_spec` |
| **Layer 2 代码生成层** | 按运行期参数，用 Jinja 渲染出**具体类型**的 `.cu` 源码，组装出 `JitSpec`。 | 各 `gen_*_module` 函数（如 `gen_act_and_mul_module`） |
| **Layer 3 编译加载层** | 把 `JitSpec` 变成 `build.ninja`，调用 ninja+nvcc 编译成 `.so`，再用 TVM-FFI 加载回 Python。 | `JitSpec.write_ninja` / `build` / `build_and_load`，`cpp_ext.run_ninja` |

注意「定义层」编号是 1 但在调用链里位于中间——这是因为 `JitSpec` 是另两层之间的**契约/数据结构**：Layer 2 生产它，Layer 3 消费它。所以先讲定义（Layer 1），再讲谁生产（Layer 2）、谁消费（Layer 3），逻辑最顺。

#### 4.1.3 源码精读

`core.py` 顶部会确保工作区存在，但刻意**不创建** `FLASHINFER_CSRC_DIR`（那是包目录，安装后可能只读）——这是 JIT 目录可写性规则的第一课：

[flashinfer/jit/core.py:18-20](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L18-L20) —— 模块导入时建好可写的工作区目录，并注释说明绝不创建只读的 `CSRC_DIR`。

工作区路径全部集中在 [env.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py) 里，按「版本号 + 排序后的 CUDA 架构」分层，`cached_ops` 放编译产物、`generated` 放生成的源码：

[flashinfer/jit/env.py:135-153](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L135-L153) —— `_get_workspace_dir_name` 用 `sorted()` 保证架构集合的目录名稳定（避免 `75_80_89` 与 `89_75_80` 造成缓存碎片），并定义 `FLASHINFER_JIT_DIR`/`FLASHINFER_GEN_SRC_DIR`。

整条链路的「粘合点」是 `JitSpec.build_and_load`，它一手牵着代码生成（拿到 spec），一手牵着编译加载（build → load），是本讲最重要的一个方法（详见 4.4 节）。

#### 4.1.4 代码实践

**目标**：在自己机器上「指认」出三层架构分别对应哪些磁盘产物，建立空间感。

**操作步骤**：

1. 运行体检命令，定位 JIT 工作区根目录（u1-l4 已介绍过 `show-config`）：
   ```bash
   flashinfer show-config
   ```
2. 在工作区根下，分别找到 `generated/` 与 `cached_ops/` 两个子目录（它们就是 Layer 2 和 Layer 3 的产物落地点）。
3. （需要 GPU，待本地验证）跑一次会触发 activation JIT 的测试，让产物真正生成：
   ```bash
   pytest tests/utils/test_activation.py::test_fused_silu_mul -x
   ```
4. 进入 `cached_ops/`，找到一个形如 `silu_and_mul/silu_and_mul.so` 的文件，以及同目录下的 `build.ninja`。

**需要观察的现象**：

- `generated/` 下出现 `silu_and_mul.cu`（Layer 2 渲染出的源码）；
- `cached_ops/silu_and_mul/` 下出现 `build.ninja`（Layer 3 的构建脚本）和 `silu_and_mul.so`（Layer 3 的最终产物）。

**预期结果**：你能用一句话把三个目录对应到三层——`include/`+`csrc/` 是只读模板（输入），`generated/` 是渲染产物（Layer 2 输出 / Layer 3 输入），`cached_ops/` 是编译产物（Layer 3 输出）。

> 若本机无 GPU 或尚未安装，第 3、4 步标为「待本地验证」；第 1、2 步可先靠阅读 [env.py:135-153](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/env.py#L135-L153) 推算出路径（默认为 `~/.cache/flashinfer/<version>/<arch>/`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FlashInfer 不干脆把所有 kernel 都 AOT 预编译好一起发版？

> **参考答案**：因为 kernel 是 C++ 模板，参数组合（dtype × head_dim × posenc × mask × backend × SM 架构）数量爆炸，预编译产物体积无法分发，且绝大多数组合用户用不到。JIT 把编译推迟到「知道真实参数」之后，按需编译并缓存，兼顾分发体积与运行性能。

**练习 2**：三层架构里，哪一层「产生」`JitSpec`，哪一层「消费」它？

> **参考答案**：Layer 2（代码生成层，各 `gen_*_module` 函数）调用 `gen_jit_spec` 产生 `JitSpec`；Layer 3（编译加载层，`JitSpec.build_and_load`）消费它。Layer 1 的 `JitSpec` 本身是两层之间的数据契约。

---

### 4.2 第一层：JitSpec——编译任务的「配料表」

#### 4.2.1 概念说明

`JitSpec`（JIT Specification）是一个 `@dataclasses.dataclass`，它把「编译一个 CUDA 模块需要的全部信息」打包成一个对象。可以把它理解成一份**配料表**：菜名（`name`）、食材（`sources`）、调味（各种 `extra_*_flags`）。只要配料表一样，做出来的菜（`.so`）就一样，因此它可以作为缓存的键。

为什么需要这样一个显式的数据结构？因为编译过程被拆成了「准备参数」和「真正编译」两步：代码生成层只负责准备配料表，编译层只负责照单做菜。这样两步可以独立测试、独立缓存，也让 CLI（`flashinfer list-modules`）能在「不真正编译」的前提下枚举出所有待编译模块。

#### 4.2.2 核心流程

一个 `JitSpec` 的生命周期：

1. **组装**：`gen_jit_spec(name, sources, extra_cuda_cflags, …)` 收集参数，补上默认 flags（`-O3`、`-use_fast_math`、各 `FLASHINFER_ENABLE_*` 宏等），构造 `JitSpec`，并登记进全局注册表。
2. **登记**：`jit_spec_registry.register(spec)` 把它记到一个 `Dict[str, JitSpec]`，供 CLI 查询状态。
3. **查询**：`spec.is_compiled` 只是检查目标 `.so` 文件是否已存在（**登记 ≠ 编译**，这是 u1-l4 已强调的点）。
4. **编译加载**：`spec.build_and_load()` 真正编译并返回 Python 模块。

`JitSpec` 的关键字段：

| 字段 | 含义 |
|------|------|
| `name` | 模块唯一标识，也是 `.so` 文件名与目录名（如 `silu_and_mul`）。 |
| `sources` | 要编译的 `.cu`/`.cpp` 文件路径列表（指向 `generated/` 下渲染好的源码）。 |
| `extra_cflags` / `extra_cuda_cflags` / `extra_ldflags` | 主机编译 / nvcc / 链接选项。 |
| `needs_device_linking` | 是否需要 `nvcc -dlink` 设备链接（涉及 device 端全局符号时）。 |

#### 4.2.3 源码精读

`JitSpec` 是一个非常薄的 dataclass，几乎没有逻辑，只有一组「派生路径」属性：

[flashinfer/jit/core.py:217-226](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L217-L226) —— `JitSpec` 的字段定义，可以看到它就是「名字 + 源文件 + 三类 flags」。

它的几个路径属性把 `name` 映射到工作区里的具体文件位置——这是理解「产物写在哪」的关键：

[flashinfer/jit/core.py:228-238](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L228-L238) —— `ninja_path`、`build_dir`、`jit_library_path` 三个属性，把模块名 `silu_and_mul` 映射到 `cached_ops/silu_and_mul/{build.ninja, silu_and_mul.so}`。

「是否已编译」纯粹是文件存在性检查——这正是 u1-l4 里 `module-status` 大片「Not Compiled」是正常现象的根源：

[flashinfer/jit/core.py:263-265](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L263-L265) —— `is_compiled` 仅判断 `.so` 文件是否存在；`get_library_path` 还会优先走 AOT 路径（u2-l2/u9-l4 详讲）。

`gen_jit_spec` 是「组装配料表 + 登记」的工厂函数，它把运行期参数和一套默认 flags 拼起来：

[flashinfer/jit/core.py:404-484](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L404-L484) —— `gen_jit_spec`：先 `check_cuda_arch()` 拒绝不支持的 GPU，再按 `FLASHINFER_JIT_DEBUG` 在 `-O0 -g`（调试）与 `-O3 -DNDEBUG`（发布）之间切换，最后 `jit_spec_registry.register(spec)`。

注意这段里对默认 flags 的处理——它解释了为什么 FlashInfer 编译出的 kernel 默认开了 `-use_fast_math`、`-DFLASHINFER_ENABLE_F16/BF16/FP8_*`：

[flashinfer/jit/core.py:431-456](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L431-L456) —— nvcc 默认 flag 集合，以及 debug/release 分支。设置 `FLASHINFER_JIT_DEBUG=1` 即可切到 `-O0 -g --device-debug -lineinfo`，这是用 `cuda-gdb`/`ncu` 调试 kernel 的开关。

#### 4.2.4 代码实践

**目标**：源码阅读型实践——给定一个模块名，你能推算出它的全部产物路径与状态。

**操作步骤**：

1. 打开 [core.py:217-265](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L217-L265)，假设有一个 `JitSpec(name="silu_and_mul", sources=[...], ...)`。
2. 手动推算：`build_dir`、`jit_library_path`、`ninja_path` 分别是什么绝对路径（结合 env.py 里的 `FLASHINFER_JIT_DIR`）。
3. 阅读注册表 [core.py:160-210](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L160-L210)，理解 `get_spec_status` 如何把 `JitSpec` 转成 `JitSpecStatus`（CLI `module-status` 用的就是它）。
4. （需要环境，待本地验证）运行 `flashinfer list-modules` 与 `flashinfer module-status`，确认 `silu_and_mul` 出现在列表里且状态符合预期。

**需要观察的现象 / 预期结果**：你能不看源码说出「`silu_and_mul` 这个模块的 `.so` 在 `<workspace>/<version>/<arch>/cached_ops/silu_and_mul/silu_and_mul.so`」。

#### 4.2.5 小练习与答案

**练习 1**：`spec.is_compiled` 返回 `False`，是否意味着这个模块「有问题」？

> **参考答案**：不是。`is_compiled` 只检查 `.so` 文件是否已存在于磁盘。JIT 是按需编译，绝大多数已登记的模块在首次调用对应 API 之前都不会编译，所以 `False` 是常态（u1-l4 的「登记 ≠ 编译」）。

**练习 2**：想用 `cuda-gdb` 调试一个 kernel，应该设哪个环境变量？它如何改变 `gen_jit_spec` 的行为？

> **参考答案**：设 `FLASHINFER_JIT_DEBUG=1`。它让 [core.py:443-456](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L443-L456) 走 debug 分支，给 nvcc 加 `-O0 -g --device-debug -lineinfo --ptxas-options=-v`，去掉 `-DNDEBUG`，从而保留调试符号与源码行映射。注意：改 flag 会改变缓存键，触发重新编译。

---

### 4.3 第二层：代码生成——gen_*_module 标准模式（以 activation 为例）

#### 4.3.1 概念说明

Layer 1 的 `JitSpec` 只描述「编译什么」，但源文件从哪来？答案就是 Layer 2 的 **代码生成层**：每个算子都有一个 `gen_*_module` 函数，它根据**编译期参数**（dtype、激活函数名、head_dim 等）现场生成一份具体的 `.cu` 源码，再把这份源码包成一个 `JitSpec`。

为什么要现场生成，而不是直接写死一份 `.cu`？因为 kernel 是模板，需要把抽象的 `DTypeIn` 替换成具体的 `half`/`nv_bfloat16`，把 `act_func_name` 替换成 `silu`/`gelu`。FlashInfer 用 **Jinja 模板**完成这种「类型特化」（type specialization）。不过 Jinja 是**可选**的——如果某个算子不需要类型特化，可以直接把现成的 `.cu` 拷过来用。

所有 `gen_*_module` 都遵循同一个**五步模式**（CLAUDE.md 里也总结了），activation 是其中最简单的范例，因此后续多讲都拿它当锚点。

#### 4.3.2 核心流程

`gen_*_module` 的标准五步：

```
1. 算 URI / 模块名   ：根据参数得到唯一标识（activation 里就是 "{act}_and_mul"）
2. 建生成目录        ：FLASHINFER_GEN_SRC_DIR（可写）
3. 渲染 Jinja（可选） ：把模板里的 {{ dtype_in }} 等占位符替换成具体类型，得到 .cu 文本
4. 写 .cu 源文件      ：用 write_if_different 写盘（内容没变就不写，避免触发重编译）
5. 返回 JitSpec       ：gen_jit_spec(name, sources, ...) → 登记并返回
```

`write_if_different` 是这里的一个关键小工具：它先读旧文件，**只有内容真的变了才写**。这样即使每次调用都「渲染 + 写」，只要渲染结果没变，文件的 mtime 就不会被无谓刷新，从而不会误导 ninja 去重编译。这是一个很值得学习的工程细节。

#### 4.3.3 源码精读

activation 的 Jinja 模板里，`{{ act_func_name }}` 和 `{{ act_func_def }}` 就是两个待替换占位符，最后用 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 导出符号供 Layer 3 加载：

[flashinfer/jit/activation.py:25-69](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L25-L69) —— `activation_templ`：渲染后得到一个完整的 `.cu` 文件，函数名 `silu_and_mul` 由 `{{ act_func_name ~ '_and_mul' }}` 拼出，kernel 本体在 `include/flashinfer/activation.cuh`（保持框架无关）。

`gen_act_and_mul_module` 就是上面五步的**最干净实现**——没有 URI 哈希、没有拷贝 `.cu`，只有「渲染 → 写 → 返回 spec」：

[flashinfer/jit/activation.py:105-117](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L105-L117) —— `gen_act_and_mul_module`：从字典取激活函数定义、渲染模板、`write_if_different` 写 `silu_and_mul.cu`、`gen_jit_spec` 返回。

`write_if_different` 的实现非常短，但它是「避免无谓重编译」的第一道防线：

[flashinfer/jit/utils.py:22-30](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/utils.py#L22-L30) —— 先比较内容，相同则直接 `return`，不同才写盘。

而 Python 侧的 `get_act_and_mul_module` 用 `@functools.cache` 把「生成 + 编译加载」整体缓存（键是 `act_func_name`），这就是 u1-l5 讲到的「进程内第一级缓存」：

[flashinfer/activation.py:40-63](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/activation.py#L40-L63) —— `@functools.cache` 装饰；首次调用执行 `gen_act_and_mul_module(...).build_and_load()`，之后同 `act_func_name` 直接命中内存缓存，**不再进入 Layer 2/3**。

#### 4.3.4 代码实践

**目标**：亲手渲染一次 activation 模板，看清「参数 → 具体 C++ 代码」这一步，**不需要 GPU**。

**操作步骤**：

1. 在已安装 flashinfer 的 Python 环境里执行（纯字符串渲染，不触发编译）：
   ```python
   from flashinfer.jit.activation import (
       get_act_and_mul_cu_str, silu_def_cu_str,
   )
   print(get_act_and_mul_cu_str("silu", silu_def_cu_str))
   ```
2. 对比打印结果与 [activation.py:25-69](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L25-L69) 的模板，确认 `{{ act_func_name }}` → `silu`、`{{ act_func_def }}` → `silu` 的 `__device__` 定义都已替换。
3. 把 `"silu"` 换成 `"gelu_tanh"`（对应 `gelu_def_tanh_cu_str`）再渲染一次，对比两份 `.cu` 的差异——差异点正是「类型特化」要解决的东西。

**需要观察的现象**：

- 渲染出的代码里，函数名变成 `silu_and_mul`，开头多了一段 `silu` 的 `__device__ __forceinline__` 定义；
- 末尾有 `TVM_FFI_DLL_EXPORT_TYPED_FUNC(silu_and_mul, silu_and_mul);`——这就是 Layer 3 加载时要找的导出符号。

**预期结果**：你亲眼看到「抽象模板 → 具体源码」的转换，并理解这一步发生在编译**之前**、运行期参数（如张量形状）并不参与渲染（只有编译期参数如激活函数名参与）。

> 说明：`get_act_and_mul_cu_str` 与各 `*_def_cu_str` 都是纯 Python 字符串工具，在 [jit/__init__.py:24-25](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/__init__.py#L24-L25) 已导出。本实践无 GPU 依赖。

#### 4.3.5 小练习与答案

**练习 1**：如果两次渲染出的 `.cu` 内容完全相同，`write_if_different` 会写盘吗？为什么这个细节很重要？

> **参考答案**：不会，它会比较内容相同后直接 return。这很重要，因为如果每次都无脑写盘会刷新文件 mtime，ninja 会据此误以为源码变了而触发重编译。`write_if_different` 是避免「无谓重编译」的第一道防线。

**练习 2**：activation 的模块名直接取 `f"{act_func_name}_and_mul"`，没有把源码哈希编进名字。这会带来什么影响？（提示：与 4.5 节的「实时重载」有关。）

> **参考答案**：模块名稳定意味着 `.so` 路径稳定，不会因为改了一个头文件就换个新目录。因此 activation 的「源码改动 → 重编译」靠的是 **ninja 的 depfile 增量构建**，而不是「换一个新 URI 重新编译一个新 `.so`」。两种机制都能实现实时重载，activation 走的是后者（详见 4.5 节）。

---

### 4.4 第三层：编译与加载——ninja + TVM-FFI

#### 4.4.1 概念说明

Layer 3 拿到 `JitSpec` 后，要做三件事：①把配料表翻译成一份 ninja 能读懂的 `build.ninja` 构建脚本；②调用 ninja 驱动 nvcc 真正编译，产出 `.so`；③用 TVM-FFI 把 `.so` 加载回 Python，拿到可调用的函数对象。

为什么用 ninja 而不是 CMake？因为 JIT 场景下，构建脚本是**每次按需临时生成**的，需要轻量、低延迟、对增量编译友好的工具。ninja 正是为「被上层工具生成」而设计的，FlashInfer 直接用 Python 字符串拼出 `build.ninja`，省去了 CMake 的 configure 阶段。

#### 4.4.2 核心流程

`JitSpec.build_and_load()` 的执行流程（带文件锁，防止多进程/多线程同时编译同一个模块）：

```
build_and_load()
  ├─ 若 is_aot（预编译包存在）→ 直接 load(aot_path)，跳过编译
  └─ 否则（JIT 路径）：
       with FileLock(lock_path):           # 进程间互斥
         ├─ build(verbose, need_lock=False)
         │    ├─ write_ninja()             # 生成 build.ninja（write_if_different）
         │    │     └─ generate_ninja_build_for_op(...)   # 拼 ninja 文本
         │    └─ run_ninja(build_dir, ninja_path, verbose)  # subprocess: ninja -C ... -f ...
         └─ load(so_path)                   # tvm_ffi.load_module(".so") → Python 模块
```

关键点：

- **AOT 短路**：如果 `flashinfer-jit-cache` 提供了预编译的 `.so`（`is_aot` 为真），直接加载、完全跳过编译——这是生产环境加速启动的机制（u9-l4 详讲）。
- **文件锁**：`build_and_load` 用 `FileLock` 包住「build + load」整体，避免「进程 A 正在编译、把旧 `.so` 删掉，进程 B 此时去 load」的竞态。注释里明确写了这个原因。
- **增量编译**：`run_ninja` 调用的是系统 `ninja`，它自带 mtime + depfile 的增量能力——没改动的 `.o` 不会重编。这是「第二次调用很快」的根本原因之一。

#### 4.4.3 源码精读

`build_and_load` 是 Layer 3 的总入口，注意它对 AOT 的短路和对锁的使用：

[flashinfer/jit/core.py:307-319](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L307-L319) —— `build_and_load`：AOT 优先直接 load；否则用同一把 `FileLock` 守住 build+load，避免他进程删 `.so` 的竞态。`verbose` 由 `FLASHINFER_JIT_VERBOSE=1` 控制。

`build` 方法本身很薄：写 ninja、跑 ninja；它还会尊重 `FLASHINFER_DISABLE_JIT`（禁用 JIT 时直接抛 `MissingJITCacheError`，要求模块必须已在缓存/AOT 里）：

[flashinfer/jit/core.py:289-302](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L289-L302) —— `build`：先检查 `FLASHINFER_DISABLE_JIT`，再 `write_ninja` + `run_ninja`。

`load` 只有一行——通过 TVM-FFI 加载 `.so`，这也是「同一份 kernel 能被多框架复用」的落点：

[flashinfer/jit/core.py:304-305](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L304-L305) —— `tvm_ffi.load_module` 把 `.so` 变成 Python 对象，其上每个属性就是 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 导出的 C++ 函数。

`build.ninja` 的内容由 `generate_ninja_build_for_op` 用字符串拼出——它定义了 `compile`/`cuda_compile`/`link` 三条 rule，并为每个源文件生成一条 build 边，最后 link 成 `.so`：

[flashinfer/jit/cpp_ext.py:238-341](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L238-L341) —— `generate_ninja_build_for_op`：注意 `cuda_compile` rule 用了 `--generate-dependencies-with-compile -MF $out.d`，这正是 4.5 节「depfile 追踪头文件」的来源。

`run_ninja` 就是构造 `ninja -v -C <workdir> -f <ninja_file>` 命令并 `subprocess.run`，`MAX_JOBS` 控制并行度：

[flashinfer/jit/cpp_ext.py:351-380](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L351-L380) —— `run_ninja`：编译失败时把 ninja 输出拼进异常信息，方便定位。

#### 4.4.4 代码实践

**目标**：源码阅读型实践——读懂一份真实 `build.ninja`，把 Layer 3 的三件事（生成脚本 / 调 ninja / 加载）对上号。

**操作步骤**：

1. 阅读 [cpp_ext.py:238-341](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L238-L341)，找出 `rule compile`、`rule cuda_compile`、`rule link` 三条规则各自的命令。
2. 回答：一个 `.cu` 源文件经过哪几条 build 边最终变成 `.so`？（提示：`.cu → .cuda.o → .so`）
3. （需要先有编译产物，待本地验证）打开 `cached_ops/silu_and_mul/build.ninja`，对照你读到的规则，验证它的结构与源码一致。
4. 设置 `FLASHINFER_JIT_VERBOSE=1` 重新触发一次编译（见 4.5 实践），观察 ninja 的 `-v` 输出里完整的 nvcc 命令行。

**预期结果**：你能解释「`generate_ninja_build_for_op` 产出的文本 → ninja 据此调用 nvcc → 产出 `.so` → `tvm_ffi.load_module` 加载」这条链。

#### 4.4.5 小练习与答案

**练习 1**：`build_and_load` 为什么要把「build」和「load」放在**同一把** `FileLock` 里，而不是分别加锁？

> **参考答案**：注释指出是为了避免一种竞态——若只锁 build，进程 A 编译完成释放锁后、可能在 load 之前，进程 B 也进入 build 并删除/替换 `.so`，导致 A load 到不一致状态。把 build+load 作为一个临界区，才能保证「我编译出来的 `.so`，就是我要 load 的那个」。

**练习 2**：设了 `FLASHINFER_DISABLE_JIT`，但某个模块既不在 AOT 包里也没编译过，调用对应 API会怎样？

> **参考答案**：`build` 会抛 `MissingJITCacheError`（[core.py:290-296](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L290-L296)）。这是「禁用 JIT 时强制要求模块必须预先存在于缓存或 AOT 包」的兜底，常用于 CI/锁定环境。

---

### 4.5 开发循环与实时重载：编辑 .cuh 后发生了什么

#### 4.5.1 概念说明

「实时重载（live reload）」是 FlashInfer 开发体验的核心卖点：你改了 `include/` 下某个 kernel 头文件里的实现，**不需要重装包、不需要重启**，下次运行测试/脚本时新代码自动生效。本节要把这个「魔法」拆开。

实时重载依赖**两个独立机制**协同：

1. **ninja 的 depfile 增量构建**：`build.ninja` 的 `cuda_compile` 规则用 `--generate-dependencies-with-compile` 生成 depfile，记录每个 `.cu` 依赖的所有头文件（包括 `include/flashinfer/*.cuh`）。当某个头文件的 mtime 变新，ninja 下次运行时就会重编对应的 `.o` 并重新 link。
2. **`@functools.cache` 的进程边界**：进程内缓存只在**单个 Python 进程**里有效。重新运行测试 = 新进程 = `get_*_module` 重新执行 = `build_and_load` 重新跑 → ninja 检测到头文件变化 → 重编译。

对于模块名**编码了源码哈希**的算子（如 attention，URI 由参数 + 源码哈希 + flags + arch 决定），改源码还会直接换一个新 `.so` 路径；而 activation 这类**名字稳定**的算子，则完全靠机制 1（ninja depfile）。两种殊途同归，都达到「改了就重编、不改就秒命中」的效果。

> 说明：URI 与源码哈希的细节属于「缓存失效」主题，是 u2-l5 的重点，本节只点到为止。

#### 4.5.2 核心流程

一次完整的「编辑 → 重载」循环：

```
开发者：在 include/flashinfer/activation.cuh 里改一行注释/实现
   │
   ▼
重新运行 pytest（= 新 Python 进程）
   │
   ▼ get_act_and_mul_module("silu")  因新进程，@functools.cache 未命中
gen_act_and_mul_module("silu")
   ├─ write_if_different 写 silu_and_mul.cu（若渲染结果没变，.cu 的 mtime 不变）
   └─ gen_jit_spec → JitSpec（名字仍是 silu_and_mul，.so 路径不变）
   │
   ▼ spec.build_and_load()
build() → run_ninja()
   ├─ ninja 读旧 depfile，发现 silu_and_mul.cu #include 了 activation.cuh
   ├─ activation.cuh 的 mtime 是新的 → 重编 silu_and_mul.cu.cuda.o
   └─ 重新 link → 覆盖 silu_and_mul.so（新实现生效）
   │
   ▼ load(.so) → 新 kernel 已就位
```

「开发循环」之所以爽，正是因为这条链路全自动：你只管改 `.cuh`，剩下的 FlashInfer + ninja 替你搞定。

#### 4.5.3 源码精读

`activation.cuh` 是框架无关的 kernel 模板，它被 Layer 2 渲染出的 `.cu` 通过 `#include <flashinfer/activation.cuh>` 引入——这就是为什么改它会触发重编译（依赖关系来自这条 include）：

[flashinfer/jit/activation.py:25-27](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/activation.py#L25-L27) —— 模板开头 `#include <flashinfer/activation.cuh>`，建立了「`.cuh` 改动 → 该 `.cu` 需重编」的依赖。

ninja 的依赖追踪能力，源自 `cuda_compile` 规则里的 depfile 生成选项：

[flashinfer/jit/cpp_ext.py:294-297](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/cpp_ext.py#L294-L297) —— `rule cuda_compile`：`--generate-dependencies-with-compile -MF $out.d` + `depfile = $out.d` + `deps = gcc`，让 ninja 知道 `.o` 依赖哪些头文件。

而测试文件本身会主动用 `build_jit_specs` 预热 JIT（避免每个 parametrize 用例都等编译），它也正是你「重新跑测试触发重载」的入口：

[tests/utils/test_activation.py:24-37](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/utils/test_activation.py#L24-L37) —— `warmup_jit` fixture 调 `build_jit_specs([...])` 批量编译三个 activation 模块；`build_jit_specs` 用一个顶层 ninja 把多个 spec 串起来并行编译（[core.py:495-516](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L495-L516)）。

最后，`flashinfer clear-cache` 会把整个 `cached_ops` 删掉，是「对比实验」的工具：

[flashinfer/__main__.py:199-206](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/__main__.py#L199-L206) —— CLI `clear-cache` 调用 `clear_cache_dir()`，对应 [core.py:111-115](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L111-L115) 的 `shutil.rmtree(FLASHINFER_JIT_DIR)`。

#### 4.5.4 代码实践（本讲主实践）

**目标**：亲手验证「改 `.cuh` → 自动重编译」，并用 `clear-cache` 对比「增量重编译」与「全量重编译」的差异。**需要 GPU 与已安装的 flashinfer（待本地验证）。**

> 注意：本实践要求你**临时修改源码做实验**，做完请用 `git checkout include/flashinfer/activation.cuh` 还原，不要把实验改动留下。

**操作步骤**：

1. 先确保有基线编译产物：跑一次 `pytest tests/utils/test_activation.py::test_fused_silu_mul -x`，让 `silu_and_mul.so` 生成。
2. 打开 [include/flashinfer/activation.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/activation.cuh)，在文件顶部注释区加一行无害注释，例如：
   ```cpp
   // JIT live-reload experiment: editing this comment should trigger recompile.
   ```
3. 开启 verbose，重跑同一个测试，**重点看 ninja 输出**：
   ```bash
   FLASHINFER_JIT_VERBOSE=1 pytest tests/utils/test_activation.py::test_fused_silu_mul -x -s
   ```
4. 还原改动：`git checkout include/flashinfer/activation.cuh`。
5. 做「全量重编译」对比：清缓存后重跑，
   ```bash
   flashinfer clear-cache
   FLASHINFER_JIT_VERBOSE=1 pytest tests/utils/test_activation.py::test_fused_silu_mul -x -s
   ```

**需要观察的现象**：

- 第 3 步（增量）：ninja 输出里应只出现 `silu_and_mul.cu` 的重编与重新 link（因为 depfile 显示它依赖被改的 `activation.cuh`），耗时应明显短于第 5 步。
- 第 5 步（全量）：`clear-cache` 删掉了所有 `.so` 与 `.o`，ninja 必须**从零编译**所有源文件，耗时显著更长。

**预期结果**：

- 你能从 ninja 的 `-v` 输出里看到一条完整的 `nvcc ... silu_and_mul.cu ...` 命令，证明 `.cuh` 的改动被捕获并触发了重编译；
- 「改一行注释 → 下次运行自动生效、无需重装包」的实时重载体验得到验证；
- 增量与全量编译的耗时差异，直观体现了两级缓存（磁盘 `.so` + ninja 增量）的价值。

> 「待本地验证」说明：ninja 是否重编取决于它读到的 depfile 是否包含 `activation.cuh` 以及该文件 mtime 是否更新——这依赖你本机的上一次成功编译（depfile 才会生成）。若第 3 步没有触发重编，可能是因为之前从未成功编译过该模块（无 depfile），此时它等同于第 5 步的全量编译。

#### 4.5.5 小练习与答案

**练习 1**：你改了 `activation.cuh` 的一行注释，但**不重新启动 Python 进程**（比如在同一个 Jupyter kernel 里再次调用 `silu_and_mul`），新代码会生效吗？为什么？

> **参考答案**：不会。因为 `get_act_and_mul_module` 上的 `@functools.cache` 在当前进程里已经命中，根本不会再进入 `build_and_load`，也就不会触发 ninja 重编译。实时重载要求「重新运行 = 新进程」。这正是两级缓存里「进程内缓存」的边界。

**练习 2**：`flashinfer clear-cache` 删掉的是 `cached_ops`（编译产物），它会删掉 `generated/` 下的 `.cu` 源码吗？这会造成什么影响？

> **参考答案**：`clear_cache_dir` 只 `rmtree(FLASHINFER_JIT_DIR)`（即 `cached_ops`），不删 `generated/`（[core.py:111-115](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/jit/core.py#L111-L115)）。因此清缓存后再次调用，Layer 2 渲染出的 `.cu`（若内容没变）依旧在，省去了重写；但所有 `.o`/`.so` 都没了，必须全量重编译——这正是 4.5.4 实践里第 5 步耗时长于第 3 步的原因。

**练习 3**：为什么说 activation 的实时重载靠「ninja depfile」，而有些算子（如 attention）靠「换一个新 URI」？两者各有什么代价？

> **参考答案**：activation 模块名稳定（`silu_and_mul`），`.so` 路径不变，靠 ninja 通过 depfile 检测头文件变化来增量重编同一份 `.so`；attention 的 URI 编码了源码哈希等，改源码会生成一个**新名字**的 `.so`（旧的不动）。前者省磁盘（不留历史 `.so`），但依赖 depfile 正确；后者实现简单、天然隔离新旧版本，但可能积累多个历史 `.so`。具体取舍与缓存失效细节见 u2-l5。

## 5. 综合实践

把本讲的三层架构串起来，完成下面这个「**给一条调用画完整注释**」的小任务：

给定用户代码：

```python
import torch, flashinfer
y = flashinfer.activation.silu_and_mul(x)   # x: cuda fp16, shape (..., 2*hidden)
```

请按下述要求产出一份「JIT 全链路追踪笔记」：

1. **分层标注**：在一张图（文字流程图即可）上标出这次调用经过的三层，并写出每层对应的源码文件与关键函数（参考 4.1.2 的总览图，但补全到函数级）。
2. **产物预测**：写出这次调用在磁盘上产生的文件（`generated/silu_and_mul.cu`、`cached_ops/silu_and_mul/{build.ninja, *.cuda.o, silu_and_mul.so}`），并指出每个文件由哪一层产生。
3. **第二次调用对比**：在**同一进程**里紧接着再调一次 `silu_and_mul(x)`，指出它停在哪一层、为什么（提示：`@functools.cache`）。
4. **改实现验证**（需要 GPU，待本地验证）：在 `include/flashinfer/activation.cuh` 的 `silu` 设备函数里临时把 `val / (1.0f + __expf(-val))` 改成 `val`（即让 silu 退化为恒等），按 4.5.4 的方法重跑测试，确认 `test_fused_silu_mul` **会失败**（因为输出不再等于参考的 silu），从而反向证明实时重载真的生效了。**做完务必 `git checkout` 还原。**

完成本任务后，你应该能不看任何资料，向别人讲清楚「FlashInfer 的一条 API 调用，从 Python 到 GPU 机器码，中间经过了哪些层、产生了哪些文件、缓存如何起作用」。

## 6. 本讲小结

- FlashInfer **默认 JIT** 是为了应对 C++ 模板 kernel 的参数组合爆炸：把「编译什么」推迟到知道真实运行期参数之后，兼顾分发体积与运行性能，代价是首次编译延迟（由两级缓存摊薄）。
- 整个 JIT 是**三层架构**：Layer 1 `JitSpec`（编译任务的「配料表」数据结构）← Layer 2 代码生成（各 `gen_*_module` 渲染 Jinja、写 `.cu`、产 `JitSpec`）→ Layer 3 编译加载（`build.ninja` + ninja/nvcc → `.so` → TVM-FFI 加载）。
- `JitSpec` 是非常薄的 dataclass；`is_compiled` 只是 `.so` 文件存在性检查，「登记 ≠ 编译」；`gen_jit_spec` 补默认 flags 并登记进全局注册表。
- `gen_*_module` 遵循统一的**五步模式**（算名/建目录/渲染 Jinja/`write_if_different` 写源码/返回 `JitSpec`），activation 是最简范例；`@functools.cache` 提供进程内第一级缓存。
- Layer 3 用 `FileLock` 守住「build+load」整体避免竞态，AOT 包存在时直接短路加载；`build.ninja` 由 `generate_ninja_build_for_op` 字符串拼接生成。
- **实时重载**靠 ninja 的 depfile 增量构建 + `@functools.cache` 的进程边界协同：改 `.cuh` 后重新运行（新进程）即自动重编译，无需重装包；`write_if_different` 与「名字稳定的 activation 靠 depfile、名字含哈希的算子靠换新 `.so`」是两道避免无谓重编译的关键设计。

## 7. 下一步学习建议

本讲是第 2 单元「JIT 编译系统」的总纲，建立了三层框架。接下来建议按依赖顺序深入：

- **u2-l2（JitSpec 与工作区环境）**：精读 `env.py` 的全部路径解析逻辑（cubin/AOT/workspace 三档优先级）、`JitSpec` 的 AOT vs JIT 路径选择，把「文件写在哪、从哪读」彻底搞清楚。
- **u2-l3（gen_*_module 代码生成模式）**：以 activation 五步法为模板，看一个**带类型特化**的生成器（如 norm 或 sampling），理解 Jinja 渲染在更复杂场景下的用法。
- **u2-l4（编译上下文与 CUDA 架构目标）**：深入 `CompilationContext`，搞懂 `TARGET_CUDA_ARCHS` 如何由 GPU 探测与 `FLASHINFER_CUDA_ARCH_LIST` 决定，以及 `supported_major_versions` 如何限制编译范围。
- **u2-l5（模块缓存与失效机制）**：本讲多次提到的「URI 编码源码哈希」「两级缓存失效」就在这里展开，是理解「什么时候会/不会重编译」的终极一讲。

如果你想跳到实战，也可以先做第 3 单元（注意力基础）的 u3-l1，在真实 attention wrapper 上再看一遍 plan/run 如何触发本讲描述的 JIT 链路；但建议至少先读完 u2-l2，否则对工作区路径的理解会有缺口。
