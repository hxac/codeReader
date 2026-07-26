# cuTile.jl Julia 内核

## 1. 本讲目标

学完本讲后，你应该能够：

- 读懂 `julia/` 目录下用 Julia 写的 cuTile.jl 内核（add / matmul / softmax），并能指出它与 Python 版 cuTile 内核在「写法」上的对应关系。
- 理解 Julia 子项目的依赖管理方式：`Project.toml` 的 `[deps]` / `[compat]`、`Pkg.instantiate()`、`--project=julia/` 的作用。
- 理解 `runtests.jl` 如何用 Julia 标准库 `Test` 组织内核正确性测试，以及它与 Python 侧 `tests/ops` 的异同。
- **本讲重点**：掌握 Python cuTile → Julia cuTile.jl 迁移时最容易踩的三类差异——0-index↔1-index、行主序↔列主序、以及随之改变的索引计算与归约轴。

> 本讲依赖 u3-l4（softmax 四种实现与 autograd 封装）。算法本身（数值稳定 softmax、分块、TMA）已在 u3-l4 讲透，本讲**不再重复算法**，只聚焦「同一种算法换一种宿主语言后，源码长什么样、索引怎么算」。

## 2. 前置知识

### 2.1 为什么 TileGym 要带一个 Julia 目录？

cuTile 的 Python 前端（`import cuda.tile as ct`，由运行时编译器 `tileiras` 编译）只是 Tile IR 的**一种前端**。社区还有一个独立的 Julia 包 [cuTile.jl](https://github.com/JuliaGPU/cuTile.jl)，它同样把内核 lowering 到同一套 Tile IR，最终编译成 GPU 代码。TileGym 在 `julia/` 目录里放了一组实验性 cuTile.jl 内核（add / matmul / softmax），用来：

1. 验证 cuTile 的 tile 编程模型可以跨宿主语言复用；
2. 给熟悉 Julia / CUDA.jl 的读者一个不依赖 Python 栈的入口。

关键认知：**`julia/` 是自包含子项目**，`import tilegym` 不会加载它，它也不参与 Python 侧的 `@dispatch` / `_REGISTRY` 分发。它独立用 Julia 自己的工具链跑测试。

### 2.2 必要的 Julia 常识（最小集）

| 概念 | 一句话说明 |
|---|---|
| 1-based 下标 | Julia 数组下标从 `1` 开始，`a[1]` 是第一个元素。 |
| 列主序（column-major）| Julia 多维数组按列连续存储，`a[i, j]` 的地址随 `i` 变化最快。 |
| 点号广播 `.` | `exp.(x)`、`x .+ y` 是逐元素运算；**不带点号的 `+` / `*` 是矩阵/标量运算**，语义不同。 |
| `!` 命名约定 | 函数名以 `!` 结尾表示「会修改参数」（in-place），如 `softmax!(out, x)`。 |
| `where {T}` | 参数化类型/函数，类似 C++ 的 `template<typename T>`。 |
| `Project.toml` | Julia 子项目的依赖清单，类比 Python 的 `pyproject.toml`。 |

> 如果你完全没写过 Julia，记住「**1-based + 列主序 + 点号广播**」这三条就够了，它们正是 cuTile.jl 与 Python cuTile 差异的根源。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [julia/Project.toml](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/Project.toml) | Julia 子项目依赖清单：声明 `CUDA` / `cuTile` / `NNlib` / `Test` 及版本兼容范围。 |
| [julia/kernels/add.jl](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/add.jl) | 最简单的逐元素内核（tensor+tensor、tensor+scalar），用来认识 cuTile.jl 内核骨架。 |
| [julia/kernels/matmul.jl](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/matmul.jl) | 分块矩阵乘内核，含 `_swizzle_2d`，是观察「1-based 与 0-based 边界转换」的最佳样本。 |
| [julia/kernels/softmax.jl](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl) | softmax 三种策略（TMA 单 tile / online 两遍 / chunked 三遍），本讲主样本。 |
| [julia/test/runtests.jl](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/runtests.jl) | 测试入口，串联 `test_add.jl` / `test_matmul.jl` / `test_softmax.jl`。 |
| [julia/test/test_softmax.jl](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/test_softmax.jl) | softmax 内核正确性测试，含 CPU 参考实现与容差断言。 |
| [src/tilegym/ops/cutile/softmax.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/softmax.py) | Python 版 softmax（u3-l4 主样本），本讲用来做逐行对照。 |

## 4. 核心概念与源码讲解

### 4.1 cuTile.jl 内核写法

#### 4.1.1 概念说明

cuTile.jl 内核的**编程模型与 Python cuTile 完全同构**（毕竟它们 lowering 到同一套 Tile IR）：

- 用「瓦片（tile）」为单位搬数据，提供 `ct.load` / `ct.store`（锚点 + 矩形形状）和 `ct.gather` / `ct.scatter`（索引数组）两套搬运原语——与 u3-l2 讲的完全对应。
- 内核里仍是 `ct.bid` / `ct.num_blocks` 取块号、`ct.arange` 生成列偏移基底、`ct.Constant` 把值标成编译期常量、`ct.launch` 启动。
- 计算**升 fp32、存回降回原精度**的精度链不变。

差别只在**宿主语言的语法外壳**：Python 用 `@ct.kernel` 装饰器 + `import cuda.tile as ct`；Julia 用 `import cuTile as ct` + 函数体内的 `ct.@compiler_options` 宏。

#### 4.1.2 核心流程

一个 cuTile.jl 内核的标准结构（以 [add.jl](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/add.jl) 为例）：

```text
主机函数 add!(output, x, y; alpha, block_size)
  ├─ n = length(x)
  ├─ grid = cld(n, block_size)          # 向上取整除法，算块数
  └─ ct.launch(add_kernel, grid, x, y, output,
               ct.Constant(alpha), ct.Constant(block_size))
       └─ 内核 add_kernel
           ├─ bid = ct.bid(1)            # 1-based 块号
           ├─ x_tile = ct.load(x; index=bid, shape=(BLOCK_SIZE,), ...)
           ├─ 计算（升 fp32 → 逐元素 → 降回 T）
           └─ ct.store(output; index=bid, tile=...)
       └─ CUDA.synchronize()             # 显式同步
```

注意 `add!` 末尾的 `CUDA.synchronize()`：cuTile.jl 的 `ct.launch` 不接收 torch stream（Julia 没有 torch），主机函数自己显式同步等待 GPU 完成。

#### 4.1.3 源码精读

**内核函数体**（逐元素加，最简骨架）：

[julia/kernels/add.jl:L19-L34](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/add.jl#L19-L34) —— `add_kernel`：取 1-based 块号 `bid`、`ct.load` 两个输入瓦片、`convert` 升 fp32、点号广播 `.+ .*` 计算、`convert` 降回 `T` 后 `ct.store`。

```julia
function add_kernel(x::ct.TileArray{T,1}, y::ct.TileArray{T,1},
                    output::ct.TileArray{T,1},
                    alpha::Float32, BLOCK_SIZE::Int) where {T}
    bid = ct.bid(1)
    x_tile = ct.load(x; index=bid, shape=(BLOCK_SIZE,), padding_mode=ct.PaddingMode.Zero)
    ...
    output_f32 = x_f32 .+ y_f32 .* alpha        # 点号 = 逐元素；alpha 是标量可省点
    ct.store(output; index=bid, tile=convert(ct.Tile{T}, output_f32))
end
```

对照几个关键映射：

| cuTile.jl（Julia） | cuTile（Python） | 说明 |
|---|---|---|
| `import cuTile as ct` | `import cuda.tile as ct` | Julia 包名 `cuTile`，Python 模块名 `cuda.tile`。 |
| `ct.TileArray{T,1}` / `ct.Tile{Float32}` | 张量参数 / `ct.float32` | Julia 用参数化类型表达「设备张量」「瓦片元素类型」。 |
| `convert(ct.Tile{Float32}, x)` | `ct.astype(x, ct.float32)` | 类型转换：Julia 用通用 `convert`，Python 用专用 `astype`。 |
| `ct.bid(1)` / `ct.num_blocks(1)` | `ct.bid(0)` / `ct.num_blocks(0)` | **维度下标：Julia 传 1，Python 传 0**（详见 4.4）。 |
| `ct.PaddingMode.Zero` / `NegInf` | `ct.PaddingMode.ZERO` / `NEG_INF` | 枚举命名：Julia CamelCase，Python UPPER_SNAKE。 |

**主机函数与启动**：

[julia/kernels/add.jl:L57-L65](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/add.jl#L57-L65) —— `add!`：用 `cld(n, block_size)`（向上取整除法）算 grid，再用 `ct.launch(kernel, grid, args...)` 启动。注意 `ct.launch` 的签名与 Python 不同（详见 4.4 对照表），且编译期常量用 `ct.Constant(alpha)` 在**调用点**标注，而非在函数签名里写 `ConstInt`。

**softmax 的三策略注释**：

[julia/kernels/softmax.jl:L5-L11](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl#L5-L11) 给出 Julia 版的三策略：TMA 单 tile / online 两遍 / chunked 三遍。其中 **online 两遍** 是 Python 版（u3-l4）没有的独立内核——它把 u3-l1/u6-l1 讲的「在线 softmax」的 m/l 修正直接用在了 softmax 上，值得细看：

[julia/kernels/softmax.jl:L59-L75](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl#L59-L75) —— Pass 1：流式地合并「行最大值 m」与「行求和 l」。每读入一个列块，先用新最大值把旧求和**平移修正**，再加入当前块：

\[
m_{\text{curr}} = \max\!\bigl(m_{\text{prev}},\ \max(x_{\text{tile}})\bigr)
\]

\[
l_{\text{curr}} = l_{\text{prev}}\cdot \exp(m_{\text{prev}}-m_{\text{curr}}) \;+\; \sum \exp(x_{\text{tile}}-m_{\text{curr}})
\]

这正是 u6-l1 Flash 注意力里「在线 softmax 合并」的公式。Pass 2（[L78-L88](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl#L78-L88)）再用最终的 m、l 把每个块归一化写出。相比之下，Python 版的 chunked（u3-l4）是 **3 遍**：先单独扫一遍 max，再单独扫一遍求和，最后写出。即 Julia 的 online =「把 max 和 sum 合并成一遍流式」，Python 的 chunked =「max、sum、写出各一遍」。两者都面向「列数 N 太大、单 tile 装不下」的场景。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：在最小内核里把「cuTile.jl 写法」和「Python cuTile 写法」对齐。

**操作步骤**：

1. 打开 [julia/kernels/add.jl:L19-L34](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/add.jl#L19-L34)。
2. 在 [src/tilegym/ops/cutile/silu_and_mul.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/silu_and_mul.py)（u4-l1 主样本）里找一段「load → astype 升 fp32 → 逐元素计算 → astype 降回 → store」的同类结构。
3. 列出两者在「类型转换」「逐元素运算符」「块号取维」三处的符号差异。

**需要观察的现象 / 预期结果**：你会看到 Python 用 `ct.astype(x, ct.float32)` + `a * b`，Julia 用 `convert(ct.Tile{Float32}, x)` + `x .+ y`。把这张映射表抄进自己的笔记。

> 实际运行 add 内核（`julia --project=julia/ -e 'using Pkg; Pkg.instantiate()'` 后跑测试）需要 **Julia 1.12+、CUDA 13.1、Blackwell GPU**，本环境无 GPU，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`add_kernel` 里 `output_f32 = x_f32 .+ y_f32 .* alpha`，如果把 `.+` 换成 `+` 会怎样？

> **答案**：`+`（不带点）在 Julia 里不是逐元素广播。对两个 `Tile`/数组用 `+` 会按矩阵加法语义处理（形状一致时可能恰好相等，但语义不同，且对「标量×数组」的 `.*` 必须带点）。cuTile.jl 内核里凡是逐元素运算都必须用点号语法，否则 lowering 阶段会报错或得到错误 IR。

**练习 2**：`ct.Constant(alpha)` 写在主机函数的 `ct.launch` 调用里，而 Python 版把常量写在内核签名的 `ConstInt` 上。这两种表达编译期常量的方式各自的好处是什么？

> **答案**：Python 版在签名上标注 `N_ROWS: ConstInt`，使「哪些参数是编译期常量」在内核定义处一目了然；Julia 版在**调用点**用 `ct.Constant(...)` 包裹，内核签名本身仍是普通 `Int`，更贴近 Julia「类型由值决定」的习惯。两者最终都把该值特化进 Tile IR，效果等价。

---

### 4.2 Julia Project 与依赖管理

#### 4.2.1 概念说明

Julia 用 **`Project.toml` + `Manifest.toml`** 管理依赖，与 Python 的 `pyproject.toml` + `uv.lock` / `poetry.lock` 几乎一一对应：

| Julia | Python 类比 | 作用 |
|---|---|---|
| `Project.toml` | `pyproject.toml` | 声明**直接依赖**与兼容范围（人写、人维护）。 |
| `Manifest.toml` | `uv.lock` / `lockfile` | 记录**完整解析后的依赖树**（含确切版本、SHA），机器生成、保证可复现。 |
| `Pkg.instantiate()` | `pip install -r requirements` / `uv sync` | 按 manifest 把依赖装齐。 |
| `--project=julia/` | 在子项目目录里操作 | 指定用哪份 `Project.toml` 作为环境。 |

`julia/` 是 TileGym 仓库里的一个**独立 Julia 环境**：它不依赖 Python 的 `tilegym` 包，只依赖 `CUDA.jl`（GPU 数组/流）、`cuTile.jl`（tile DSL）和 `NNlib.jl`（个别参考算子）。

#### 4.2.2 核心流程

```text
进入 julia 环境
  ├─ julia --project=julia/ -e 'using Pkg; Pkg.instantiate()'
  │       └─ 读 Project.toml 的 [deps]/[compat]，按 Manifest.toml 解析安装
  └─ julia --project=julia/ julia/test/runtests.jl
          └─ 在 julia/ 环境里 import CUDA / cuTile，跑全部 test_*.jl
```

`[compat]` 段是 Julia 的版本兼容声明，语义是 [SemVer range](https://semver.org/)：`"5.9"` 表示「`>=5.9.0, <6.0.0`」（Julia Pkg 默认 caret 语义）。

#### 4.2.3 源码精读

[julia/Project.toml:L13-L23](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/Project.toml#L13-L23) —— 直接依赖只有四个，兼容范围紧扣 TileGym 的硬件要求（CUDA 13.1 / Blackwell）：

```toml
[deps]
CUDA   = "052768ef-5323-5732-b1bb-66c8b64840ba"
NNlib  = "872c559c-99b0-510c-b3b7-b6c96a88d5cd"
cuTile = "0dea8319-8c4a-4662-a73d-20234d115b9a"
Test   = "8dfed614-e22c-5e08-85e1-65c5234f0b40"

[compat]
CUDA   = "5.9"
cuTile = "0.2"
NNlib  = "0.9"
julia  = "1.12"
```

几个要点：

- 每个 dep 旁边那串十六进制是 **UUID**——Julia 用 UUID（而非包名字符串）唯一标识包，因为不同注册表可能有同名包。
- `Test` 是 Julia 标准库，但仍需在 `Project.toml` 里显式列出（标准库也要「预订」）。
- `julia = "1.12"` 限定 Julia 语言版本本身；仓库的 [Manifest.toml](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/Manifest.toml#L7-L9) 记录的实际解析结果是 `1.12.5`。
- README 给出的安装/运行命令与 Project.toml 严格对应：[README.md:L140-L155](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/README.md#L140-L155)。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：把 Julia 依赖管理映射到你熟悉的 Python 工作流。

**操作步骤**：

1. 读 [julia/Project.toml:L13-L23](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/Project.toml#L13-L23)，找出 cuTile.jl 的兼容版本范围。
2. 打开 [julia/Manifest.toml](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/Manifest.toml)，搜索 `cuTile` 实际解析到的确切版本。
3. 对照 [pyproject.toml](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/pyproject.toml) 里 Python 侧 `cuda-tile` 的版本要求（u1-l2 讲过 `cuda-tile >= 1.5.0`）。

**预期结果**：你会看到 `[compat]` 的 `"0.2"` 与 Manifest 里某个 `0.2.x` 确切版本对应，关系等同于 `uv.lock` 锁定 `pyproject.toml` 里声明的范围。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `julia/` 要带一份 `Manifest.toml` 进 git，而很多 Python 项目会把 `uv.lock` / lockfile 也提交？

> **答案**：为了**可复现性**。Manifest/lock 记录的是「完整依赖树的确切版本与哈希」，有了它，任何人 `Pkg.instantiate()` 都能得到与作者完全一致的依赖；没有它，Pkg 会按 `[compat]` 范围重新解析，可能拿到不同的次版本，导致行为漂移。TileGym 把 `Manifest.toml` 提交，正是为了让 Julia 子项目的测试在 CI 与本地一致。

**练习 2**：`--project=julia/` 这个启动参数如果不加，会发生什么？

> **答案**：不加就会用「全局默认环境」（通常是 `~/.julia/environments/v1.12`）而非 `julia/Project.toml`。那样 `using cuTile` / `using CUDA` 找不到这些依赖（除非全局环境恰好也装了），于是报 `ArgumentError: Package ... not found`。`--project=PATH` 的作用就是把当前环境的 `Project.toml` 指到 PATH。

---

### 4.3 runtests 测试体系

#### 4.3.1 概念说明

Julia 内核的正确性测试用**标准库 `Test`**（`@testset` / `@test`），与 Python 侧用 pytest（u9-l1 的 `PyTestCase` + `assertCorrectness`）是两套完全独立的体系。但**测试思想完全一致**：

1. 准备一个 CPU 参考（reference）；
2. 把输入搬到 GPU（`CuArray`）、跑内核；
3. 把结果搬回 CPU（`Array(...)`），与参考做容差比较。

差别只是语法：Julia 用 `@test a ≈ b atol=.. rtol=..`（`≈` 是 `isapprox` 的中缀写法），Python 用 `torch.allclose(a, b, atol=.., rtol=..)`。

#### 4.3.2 核心流程

```text
runtests.jl（入口）
  └─ @testset "TileGym Julia Kernels"
       ├─ include("test_add.jl")
       ├─ include("test_matmul.jl")
       └─ include("test_softmax.jl")     ← 每个 test_*.jl 自带 @testset

test_softmax.jl 内部
  ├─ include("../kernels/softmax.jl")   ← 直接把内核源码 include 进来（无需装包）
  ├─ reference_softmax(x)               ← CPU 参考实现
  └─ @testset 分组：TMA / online / chunked / 数值稳定性 / 单行
       └─ 每个 case: randn → CuArray → softmax_xxx!(out, x) → @test Array(out) ≈ expected
```

一个关键细节：测试用 `include(joinpath(KERNEL_DIR, "softmax.jl"))` **直接把内核源文件包含进来**，而不是 `using cuTile.SomeModule`。因为 `julia/kernels/*.jl` 只是普通脚本（没有定义 `module`），`include` 后其顶层函数（`softmax_tma!` 等）就直接可用。

#### 4.3.3 源码精读

**入口**：

[julia/test/runtests.jl:L8-L21](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/runtests.jl#L8-L21) —— 注释写明用法（`julia --project=julia/ julia/test/runtests.jl`）与前置（Julia 1.12+、CUDA.jl、cuTile.jl），顶层一个 `@testset` 依次 `include` 三个子测试文件。

```julia
@testset "TileGym Julia Kernels" begin
    include(joinpath(TEST_DIR, "test_add.jl"))
    include(joinpath(TEST_DIR, "test_matmul.jl"))
    include(joinpath(TEST_DIR, "test_softmax.jl"))
end
```

**CPU 参考**：

[julia/test/test_softmax.jl:L16-L26](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/test_softmax.jl#L16-L26) —— `reference_softmax`：对列主序 `(M, N)` 矩阵，逐行（`x[i, :]`）做数值稳定 softmax。注意 `x[i, :]` 取的是「第 i 行所有列」，在**列主序下这是非连续切片**（步长为 M），但 CPU 参考只求正确、不在乎布局。

**容差断言**：

[julia/test/test_softmax.jl:L54-L58](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/test_softmax.jl#L54-L58) —— TMA 小 N 用例：`@test Array(out_gpu) ≈ expected atol=1e-5 rtol=1e-4`。对照 Python 侧（u1-l3/u9-l1）softmax 的 fp32 容差 `rtol=1e-5, atol=1e-7`——Julia 侧的 rtol 放得略宽（`1e-4` vs `1e-5`），因为 online/chunked 对大 N 的多块累加误差更大（见 [L75](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/test_softmax.jl#L75) 与 [L93](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/test_softmax.jl#L93) 给到 `atol=1e-4 rtol=1e-3`）。matmul 因走 TF32 张量核心，容差更宽 `atol=1e-1 rtol=1e-2`（[test_matmul.jl:L29](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/test_matmul.jl#L29)）。

**数值稳定性用例**：

[julia/test/test_softmax.jl:L97-L115](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/test_softmax.jl#L97-L115) —— 把输入乘 `100f0`（极大值），断言结果全有限（`isfinite`）、全非负、每行和为 1。这正好验证 u3-l4 讲的「减最大值」数值稳定技巧在 Julia 版里也生效。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：理解 Julia 测试如何「不装包也能测内核」。

**操作步骤**：

1. 读 [julia/test/test_softmax.jl:L10-L11](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/test/test_softmax.jl#L10-L11) 的 `include(joinpath(KERNEL_DIR, "softmax.jl"))`。
2. 思考：为什么这里用 `include` 而不是 `using cuTile`？`softmax_tma!` 定义在哪个文件、被包含后它的可见范围是什么？
3. 对照 Python 侧 [tests/ops/test_softmax.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_softmax.py)：它必须 `import tilegym.ops` 走完整分发链路（u9-l1 讲过），而 Julia 版**绕过了分发**，直接测内核函数本身。

**预期结果**：你能说清「Julia 测试 = 直接 include 内核脚本 + CPU 参考逐行对比」，与 Python 测试「必须经过 `@dispatch`→`_REGISTRY` 路由」是两种不同取舍——Python 测的是「端到端算子」，Julia 测的是「裸内核」。

> 实际跑 `julia --project=julia/ julia/test/runtests.jl` 需要 GPU，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`@test Array(out_gpu) ≈ expected atol=1e-5 rtol=1e-4` 里的 `Array(out_gpu)` 起什么作用？不写会怎样？

> **答案**：`out_gpu` 是 `CuArray`（GPU 显存），`Array(...)` 把它**搬回 CPU 主存**，这样 `≈`（`isapprox`）才能在 CPU 侧逐元素比较。不写的话，`isapprox` 对 `CuArray` 可能没有定义或触发 GPU 内核去比较，行为不可预期；正确做法永远是先 `Array(...)` 搬回再比。

**练习 2**：为什么 online/chunked 用例（大 N）的容差比 TMA（小 N）宽？

> **答案**：大 N 时一行被切成多个列块，max/sum 要跨块累加；每次 `exp`、归约都会引入微小舍入误差，块数越多误差累积越大，故容差放宽到 `rtol=1e-3`。TMA 单 tile 一次读完，无跨块累加，误差小，容差收得紧（`rtol=1e-4`）。

---

### 4.4 与 Python cuTile 的关键差异（核心对照）

> 本模块是本讲的**重头戏**，直接服务于综合实践任务。差异归结为三组：**索引约定（0 vs 1）**、**内存布局（行 vs 列主序）**、**API 语法外壳**。

#### 4.4.1 概念说明

**① 索引约定：0-based ↔ 1-based**

这是迁移时**最容易出错**的一点。但要注意：cuTile.jl 里的索引约定**不是全局统一**的，而是「按原语、按调用点」分两套：

- 跟随 Julia 1-based 约定的：`ct.bid(1)` / `ct.num_blocks(1)` 取维度；`ct.load` 的锚点 `index=(row_idx, Int32(1))`（第一列 = 1）；列块循环 `for col_idx in Int32(1):num_col_tiles`。
- 沿用 GPU / C 的 0-based 偏移的：`ct.arange(TILE_SIZE)` 生成 `[0,1,...,TILE_SIZE-1]` 的偏移基底；由它拼出的 `gather` 列索引数组；以及 `_swizzle_2d` 这个辅助函数。

matmul 内核把这道边界写得最清楚——见 4.4.3。结论：**在 cuTile.jl 里，每遇到一个索引，都要先判断它属于「1-based 锚点世界」还是「0-based 偏移世界」，必要时手动 ±1 转换**。

**② 内存布局：行主序 ↔ 列主序**

- Python torch 张量是**行主序**，`shape=(n_rows, n_cols)`，最后一维（列）连续。softmax 对最后一维归约 = 沿连续维归约。
- Julia `CuMatrix` 是**列主序**，`size(x)=(M,N)`，第一维（行）连续。softmax 仍对 `dims=2`（列维）归约，但在列主序下「一行」是跨列的**非连续、按步长 M 的访问**——好在 `ct.load`/TileArray/TMA 抽象掉了物理布局，写法不变，但底层访存模式与 Python 版不同。
- matmul 受布局影响最直接：Julia 版注释明确写 `A(M,K), B(K,N), C(M,N)`（[matmul.jl:L7-L9](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/matmul.jl#L7-L9)），是标准 Julia `A*B` 的自然形状；而 Python 版按行主序理解同样的形状。

**③ API 语法外壳**

内核模型同构，但调用语法差异不少——见 4.4.2 的总对照表。

#### 4.4.2 核心流程（总对照表）

下表把本讲涉及的所有差异收拢成一张「迁移速查表」：

| 维度 | Python cuTile（softmax.py / matmul.py） | cuTile.jl（softmax.jl / matmul.jl / add.jl） | 影响 |
|---|---|---|---|
| 维度下标 | `ct.bid(0)` / `ct.num_blocks(0)` | `ct.bid(1)` / `ct.num_blocks(1)` | block 号取维：0-based vs 1-based |
| load 锚点（首列） | `index=(row_idx, 0)` | `index=(row_idx, Int32(1))` | 第一列坐标：0 vs 1 |
| 列块循环 | `range(num_chunks)`（0..n-1） | `Int32(1):num_col_tiles`（1..n） | 循环上下界整体 +1 |
| gather 列偏移基底 | `ct.arange(TILE_SIZE)` = 0-based | `ct.arange(TILE_SIZE)` = 0-based（**相同**） | gather 索引数组两侧一致 |
| 归约轴（1D 瓦片） | `ct.max(row, 0, keepdims=True)` | `maximum(chunk)`（标量，再包 `ct.Tile`） | 风格不同，效果相当 |
| 归约轴（2D (1,N) 瓦片） | `ct.max(row, 1, keepdims=True)` | `maximum(row; dims=2)` | 两者都沿「列维」 |
| 持久化循环 | `for row_idx in range(pid, N_ROWS, num_programs)` | `while row_idx <= n_rows; ...; row_idx += num_programs` | Python range ↔ Julia while |
| occupancy 提示 | `@ct.kernel(occupancy=4)`（装饰器参数） | 函数体内 `ct.@compiler_options occupancy=2`（宏） | 写法位置不同 |
| 类型转换 | `ct.astype(x, ct.float32)` | `convert(ct.Tile{Float32}, x)` | API 名不同 |
| TF32 | `ct.astype(a, ct.tf32)` | `convert(ct.Tile{ct.TFloat32}, a)` | 类型名 `tf32` vs `TFloat32` |
| 逐元素运算 | `a - b` / `exp(x)`（numpy 风格自动广播） | `a .- b` / `exp.(x)`（**必须点号**） | 漏点号即语义错误 |
| 启动签名 | `ct.launch(stream, grid_tuple, kernel, args_tuple)` | `ct.launch(kernel, grid_int, args...)` | Julia 不传 stream、grid 是整数、args 散列 |
| 显式常量 | 签名标注 `ConstInt` | 调用点 `ct.Constant(v)` | 标注位置不同 |
| 同步 | 不显式 sync（随 torch stream） | `CUDA.synchronize()` | Julia 主机函数显式等 |
| 布局 | 行主序 `(n_rows, n_cols)` | 列主序 `(M, N)` | 访存模式不同（见上） |
| autograd | `torch.autograd.Function` 包前向（u3-l4/u4-l2） | **无 autograd**，`softmax!` 仅前向 in-place | 反向需自己写或不要 |
| 输出分配 | 主机 `torch.empty_like(x)` | 调用方传入预分配 `output`（`!` 约定） | 谁分配谁负责 |

#### 4.4.3 源码精读（差异的「现场证据」）

**(a) 1-based ↔ 0-based 边界转换：matmul 的 swizzle**

[julia/kernels/matmul.jl:L39-L47](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/matmul.jl#L39-L47) —— 这是观察索引约定最清楚的一段：

```julia
bid = ct.bid(1)                       # 1-based 块号（Julia 约定）
...
# swizzle_2d expects 0-indexed bid, returns 0-indexed tile coords
bid_m_0, bid_n_0 = swizzle_2d(M, N, tm, tn, 8, bid - Int32(1))  # bid-1 → 0-based
bid_m = bid_m_0 + Int32(1)            # +1 → 回到 1-based 给 load 用
bid_n = bid_n_0 + Int32(1)
...
num_k = ct.num_tiles(A, 2, (tm, tk))  # dim=2（1-based 第 2 维 = K）
for k in Int32(1):num_k               # 1..num_k（Julia 闭区间）
    a = ct.load(A; index=(bid_m, k), shape=(tm, tk), ...)
```

对照 Python 版 [src/tilegym/ops/cutile/matmul.py:L174-L180](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L174-L180)：Python 直接 `bidx, bidy = _swizzle_2d(M, N, ...)`（`bid` 本就是 0-based，无需 ±1），`num_tiles_k = ct.num_tiles(A, axis=1, ...)`（axis=1 即第 2 维，但用 0-based）。**同一算法，Julia 版多出两处 `±1` 转换，这就是 1-based 宿主语言带来的迁移成本**。

**(b) load 锚点的首列坐标**

[julia/kernels/softmax.jl:L30-L31](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl#L30-L31) —— TMA 单 tile：`ct.load(input; index=(row_idx, Int32(1)), shape=(1, TILE_SIZE), ...)`，第二坐标 `Int32(1)` = 第一列。对照 Python 版 [src/tilegym/ops/cutile/softmax.py:L94](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/softmax.py#L94) 的 `index=(row_idx, 0)`（第一列 = 0）。一字之差，含义差一列。

**(c) 持久化循环：range ↔ while**

[julia/kernels/softmax.jl:L28-L42](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl#L28-L42) —— TMA 持久化用 `while row_idx <= n_rows ... row_idx += num_programs`。对照 Python 版 [src/tilegym/ops/cutile/softmax.py:L31](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/softmax.py#L31) 的 `for row_idx in range(pid, N_ROWS, num_programs)`。两者都是 u3-l3 讲的「静态持久化 grid-stride」，只是循环语法不同；注意 Python `range` 是**左闭右开**、Julia `while <=` 是**闭区间**，终止条件要相应调整。

**(d) gather 索引数组：两侧一致地 0-based**

[julia/kernels/softmax.jl:L105-L113](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl#L105-L113) —— chunked 版：`col_offsets_base = ct.arange(TILE_SIZE)`，再 `col_indices = ct.broadcast_to(ct.Tile(chunk_idx * Int32(TILE_SIZE)), (TILE_SIZE,)) .+ col_offsets_base`，其中 `chunk_idx` 跑 `0:num_chunks-1`。这与 Python 版 [src/tilegym/ops/cutile/softmax.py:L135-L140](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/softmax.py#L135-L140) 的 `col_offsets_base = ct.arange(TILE_SIZE, ...)` + `for chunk_idx in range(num_chunks)` **逐字一致**——证明 `gather` 的列索引数组在两侧都是 0-based 偏移。这正是上面强调的「索引约定按原语分两套」的实证：同一份 softmax.jl 里，`load` 用 1-based 锚点、`gather` 用 0-based 数组。

**(e) 启动签名差异**

[julia/kernels/softmax.jl:L154-L160](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl#L154-L160) —— Julia `ct.launch(softmax_kernel_tma, M, output, input, ct.Constant(tile_size))`：第一参是 kernel、第二参 grid 是整数 `M`、之后是散列参数。对照 Python 版 [src/tilegym/ops/cutile/softmax.py:L193-L204](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/softmax.py#L193-L204) 的 `ct.launch(torch.cuda.current_stream(), grid, kernel, (output, input, ...))`：第一参是 stream、grid 是元组、kernel 第三、参数打包成元组。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：亲手把一个差异点在两侧源码里定位、对照。

**操作步骤**：

1. 打开 [julia/kernels/matmul.jl:L51-L60](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/matmul.jl#L51-L60) 的 K 维循环。
2. 在 Python 版 [src/tilegym/ops/cutile/matmul.py:L194-L207](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L194-L207) 找对应循环。
3. 对比三点：K 块循环上下界（`1:num_k` vs `range(num_tiles_k)`）、TF32 转换写法（`convert(ct.Tile{ct.TFloat32}, a)` vs `a.astype(...)`）、矩阵乘加（`muladd(a, b, acc)` vs `ct.mma(a, b, accumulator)`）。

**预期结果**：你能在不运行代码的情况下，逐行把 Julia 版翻译回 Python 版，并指出每个符号的差异来源（语言语义 / cuTile.jl API 设计）。

#### 4.4.5 小练习与答案

**练习 1**：matmul.jl 里 `bid_m_0, bid_n_0 = swizzle_2d(M, N, tm, tn, 8, bid - Int32(1))` 为什么传 `bid - 1` 而不是 `bid`？如果忘改会怎样？

> **答案**：`ct.bid(1)` 返回的是 **1-based** 块号，而 `_swizzle_2d` 这个辅助函数（与 Python 版共用同一套 0-based 的 super-grouping 算法，[matmul.jl:L16-L26](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/matmul.jl#L16-L26)）按 0-based 的 `bid` 做整除/取模分组。传 `bid` 会让 swizzle 从 1 而非 0 起算，分组错位，最后一块 CTA 可能算到不存在的 tile（越界）或漏算边缘 tile。所以必须 `bid - 1` 转入 0-based，算完再 `+1` 回 1-based 给 `load` 用。

**练习 2**：softmax 对「行」做归约。在 Python（行主序）和 Julia（列主序）里，哪一侧的「一行」在显存里是连续的？这对性能意味着什么？

> **答案**：**Python 侧连续，Julia 侧不连续**。行主序下，一行的 N 个列元素地址相邻，softmax 沿行归约 = 沿连续维归约，访存友好；列主序下，一行的 N 个元素跨列、步长为 M（行数），是 strided 访问，对缓存/TMA 不友好。好在 cuTile 的 `ct.load` 返回的是逻辑瓦片，TMA/Tile IR 会处理物理布局，写法不变；但**底层访存模式不同，性能特征也不同**——这是把内核从 Python 搬到 Julia 时除了「改语法」之外，唯一需要在心里记一笔的非语法差异。

---

## 5. 综合实践

把本讲全部内容串起来：**亲手做一份《Python cuTile ↔ cuTile.jl softmax 迁移对照报告》**。

### 实践目标

证明你已经能独立读懂任一侧的 softmax 内核，并能在两者之间互译。

### 操作步骤

1. **左右分屏**打开两个文件：
   - 左：[julia/kernels/softmax.jl](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl)（TMA 策略 `softmax_kernel_tma`，L20-L44）
   - 右：[src/tilegym/ops/cutile/softmax.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/softmax.py)（TMA 策略 `_softmax_kernel_tma`，L80-L114）

2. **填这张迁移表**（在每一行写出两侧的具体代码片段）：

   | 对照点 | Python 片段 | Julia 片段 | 差异性质 |
   |---|---|---|---|
   | 取块号（维下标） | `ct.bid(0)` | ? | 0 vs 1-based |
   | load 的首列坐标 | `index=(row_idx, 0)` | ? | ? |
   | 持久化循环 | `for row_idx in range(pid, N_ROWS, num_programs)` | ? | range vs while |
   | 归约轴 | `ct.max(row, 1, keepdims=True)` | ? | axis 1 vs dims 2 |
   | 类型升 fp32 | `ct.astype(row, ct.float32)` | ? | ? |
   | 逐元素减/除 | `row - row_max` / `numerator / denominator` | ? | 是否点号 |
   | 类型降回 | `ct.astype(..., input.dtype)` | ? | ? |
   | occupancy 提示 | `@ct.kernel(occupancy=2)` | ? | 装饰器 vs 宏 |
   | 主机启动 | `ct.launch(stream, grid, kernel, (args,))` | ? | 元组 vs 散列 |

3. **回答三个关键问题**（用源码行号佐证）：
   - 为什么 matmul 里要写 `bid - Int32(1)` → swizzle → `+ Int32(1)`，而 softmax 的 TMA 版不需要这种 ±1？（提示：softmax 用的是 `ct.bid(1)` 直接当行号，没有经过 0-based 的 swizzle 辅助函数。）
   - `ct.arange(TILE_SIZE)` 在 Julia chunked 版（[L105](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl#L105)）产生的偏移是 0-based 还是 1-based？这与 `ct.load` 锚点的 1-based 是否冲突？说明「索引约定按原语分两套」在你的代码里如何体现。
   - 列主序对 softmax 的「逐行归约」在物理访存上意味着什么？为什么这不会改变内核的写法？

4. **（可选，需 GPU）实跑验证**：
   ```bash
   julia --project=julia/ -e 'using Pkg; Pkg.instantiate()'
   julia --project=julia/ julia/test/runtests.jl
   ```
   观察三个 `@testset`（add / matmul / softmax）是否全绿。本环境无 Julia、无 GPU，**待本地验证**。

### 需要观察的现象 / 预期结果

- 第 2 步的迁移表应能填满，且每行的「差异性质」都能归入 4.4.2 总表的三类（索引约定 / 布局 / 语法外壳）之一。
- 第 3 步能用具体行号（如 matmul 的 [L43-L45](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/matmul.jl#L43-L45)）举证，而不是泛泛而谈。
- 第 4 步若能跑，softmax 三个子测试（TMA / online / chunked）应在各自的容差内通过。

## 6. 本讲小结

- `julia/` 是 TileGym 里**自包含的 Julia 子项目**，用独立的 cuTile.jl 前端把内核 lowering 到与 Python cuTile 相同的 Tile IR；它不参与 Python 侧的 `@dispatch` / `_REGISTRY` 分发，独立用 `Test.jl` 跑测试。
- **编程模型同构、语法外壳不同**：内核仍是 tile 搬运 + `ct.bid` + `ct.load/gather` + `ct.launch`，但类型转换用 `convert`、逐元素运算必须加点号、occupancy 用函数体内宏、常量在调用点用 `ct.Constant` 标注。
- **索引约定按原语分两套**：`bid` / `load` 锚点 / 列块循环跟 Julia 的 1-based；`arange` 偏移 / `gather` 索引数组 / `swizzle_2d` 沿用 0-based——matmul 的 `bid-1 → swizzle → +1` 是这套边界转换最清楚的范例。
- **内存布局不同**：Python 行主序、Julia 列主序；softmax 对「行」归约在两侧的物理访存连续性相反，但 `ct.load` 抽象掉了布局，写法不变。
- **依赖与测试**：`Project.toml`/`Manifest.toml` 类比 `pyproject.toml`/`uv.lock`，`Pkg.instantiate()` 对应装依赖；测试用 `include` 直接包含内核脚本、`@test a ≈ b atol=.. rtol=..` 做 CPU 参考对比，比 Python 侧更「裸」、绕过分发直接测内核。
- **没有 autograd**：Julia 版 `softmax!` 是 in-place、仅前向；Python 版的 `torch.autograd.Function` 反向封装（u3-l4/u4-l2）在 Julia 侧没有对应物。

## 7. 下一步学习建议

- **横向巩固多后端思想**：本讲的 cuTile.jl 是「同一算法、换宿主语言」的样本；接着读 u7（tilecpp / triton / cutile-rs）看「同一算子名、换实现语言与编译路径」的后端族，体会 TileGym「算子名是全局键、实现语言无关」的设计。
- **深入 matmul 的调度**：本讲 matmul 只展示了 swizzle 的索引转换；完整的持久化 grid-stride、CGA/`num_ctas`、autotune 在 u5-l2/u5-l3 讲透，可对照 Python 版 [src/tilegym/ops/cutile/matmul.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py) 阅读。
- **动手迁移一个内核**：参考仓库内的 skill `tilegym-converting-cutile-to-julia`（见 `.claude/skills/`），它会给出 0-index→1-index、行列主序、广播差异的系统化迁移清单。可尝试把 Python 版 [silu_and_mul.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/silu_and_mul.py)（u4-l1）迁移成 cuTile.jl，作为毕业练习。
- **在线 softmax 的延伸**：本讲 4.1 提到的 online softmax 的 m/l 修正公式，在 u6-l1（FMHA）里被用来做完整的 Flash 注意力分块合并，建议读完 u6-l1 再回看 [softmax.jl 的 Pass 1](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/julia/kernels/softmax.jl#L59-L75)，体会「同一公式如何从归一化层复用到注意力层」。
