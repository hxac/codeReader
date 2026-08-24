# 第 3 讲：运行环境与内核运行方式

## 1. 本讲目标

学完本讲，你应该能够：

1. 正确安装并验证 TIRx 编译器——它不是独立的包，而是 Apache TVM wheel 中的 `tvm.tirx` 模块，且需要与 `cuda-bindings` 配套安装。
2. 判断手头的 GPU 是否满足书中内核的 `sm_100a`（Blackwell）要求，并理解为什么别的 GPU 跑不了。
3. 掌握贯穿全书的内核验证套路：用 PyTorch 张量直接调用编译产物，再与 PyTorch 参考结果做数值断言。
4. 记住一条容易踩坑的规则：TIRx 通过 Python 源码检视解析内核，因此内核代码必须写在文件或 notebook 单元格里，不能塞进 `python -c`。
5. 了解可选的 `tirx-kernels` 参考内核仓库及其固定 revision 的用意。

本讲是「环境课」：它不教你写内核，但之后每一讲的可运行实践都建立在本讲搭好的环境之上。

## 2. 前置知识

本讲需要的背景知识都很轻量，用通俗语言过一遍：

- **pip 与 Python 环境**：本书工具链通过 `pip install` 安装。建议在一个干净的虚拟环境（`venv` 或 `conda`）中操作，避免和系统里其他 PyTorch/TVM 冲突。
- **什么是编译器 wheel**：Apache TVM 是一个编译器项目，它的 Python 包（`apache-tvm`）里同时包含 Python 前端和编译器后端的动态库。我们说的 TIRx 是这个包里的一个模块（`tvm.tirx`），不需要单独安装。
- **NVRTC**：NVIDIA Runtime Compilation 的缩写，即在运行时把 CUDA C 源码编译成可加载的设备代码。TVM 生成 CUDA 源码后走这条路径，而 Python 侧调用 NVRTC 需要额外的绑定包——这就是 `cuda-bindings` 存在的原因。
- **GPU 架构标记（`sm_xx`）**：NVIDIA 每代 GPU 有一个架构标记，如 Hopper 是 `sm_90`，Blackwell 数据中心 GPU 是 `sm_100a`（后缀 `a` 表示启用该架构的专属指令集）。书中内核大量使用 `tcgen05.mma`、Tensor Memory（TMEM）等 **Blackwell 专属** 硬件特性，所以老 GPU 即使能装上工具链，也无法运行这些内核。
- **PyTorch 张量**：书中的输入数据和参考答案都用 PyTorch 张量表示。你只需要会 `torch.randn`、`torch.zeros`、矩阵乘 `@` 和 `torch.testing.assert_close` 这几个基本操作。
- **与前一讲的衔接**：u1-l2 讲过，本地构建这本书的站点**不需要** GPU、也不需要 tvm——构书只是 Sphinx 渲染 Markdown。本讲开始要求另一套东西：**运行书中内核**的环境。两件事容易混淆，请分开对待。

## 3. 本讲源码地图

本讲涉及的关键文件只有两个，它们分别回答「环境怎么装」和「装好后内核怎么跑、怎么验证」：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md) | 仓库首页。其中 "Running the kernels" 一节是官方的运行环境安装说明：TIRx 编译器、PyTorch、可选的 tirx-kernels 三步，以及「示例必须写在文件里」的规则。 |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | 第一个 TIRx 内核 `hgemm_v1` 所在章节。章首的 "Running the examples" 提示框复述了环境要求并解释了 `cuda-bindings` 的必要性；章节中段给出完整的「编译 + PyTorch 验证」代码，是本讲第 3 个模块的主要依据。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① apache-tvm 安装；② sm_100a 硬件要求；③ 编译与验证回路（PyTorch 张量直接调用）；④ tirx-kernels 参考内核。

### 4.1 apache-tvm 安装：TIRx 编译器从哪里来

#### 4.1.1 概念说明

第一个要纠正的直觉是：「TIRx 是一个独立工具」——不对。**TIRx 以 `tvm.tirx` 模块的形式随 Apache TVM 的 wheel 一起发布**。也就是说，安装 TIRx 编译器 = 安装指定版本的 `apache-tvm`。

第二个要点是**为什么还要装 `cuda-bindings`**。TIRx 内核编译的最后一站是 CUDA C 源码，TVM 通过 NVRTC 在运行时编译它；Python 侧调用 NVRTC 需要额外的绑定库，即 `cuda-bindings`。所以这两个包总是一起出现。

第三个要点是**版本必须钉死**。官方命令使用 `apache-tvm==0.26.0`，这是一个精确版本号。TIRx 是快速演进中的新模块，不同 TVM 版本之间的 API 和行为可能有差异，跟随书中指定的版本能最大程度避免「照着书敲却报错」。

#### 4.1.2 核心流程

安装与验证的完整流程：

```text
1. （建议）创建并激活一个干净的虚拟环境
2. pip install apache-tvm==0.26.0 cuda-bindings
3. python -c "import tvm, tvm.tirx; print(tvm.__version__)"
4. 看到版本号打印且无 ImportError → TIRx 编译器就绪
5. （第 2 个模块）安装 CUDA 版 PyTorch
```

注意第 3 步只做「导入 + 打印版本」——它验证的是**编译器可用**，还不需要 GPU。

#### 4.1.3 源码精读

仓库 README 的 "Running the kernels" 一节给出了权威安装步骤。第一步明确说明 TIRx 的发布形态：

> **1. Install the TIRx compiler.** It ships as the `tvm.tirx` module of the Apache TVM wheel:

对应源码：[README.md:L56-L66](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L56-L66)

这段先说明「TIRx 编译器以 Apache TVM wheel 中的 `tvm.tirx` 模块形式发布」，随后给出安装命令 `pip install apache-tvm==0.26.0 cuda-bindings`，最后给出验证命令 `python -c "import tvm, tvm.tirx; print(tvm.__version__)"`。

`cuda-bindings` 的必要性在 TIRx 入门章节的 "Running the examples" 提示框里有更明确的解释：

> Compiling CUDA through NVRTC also requires `cuda-bindings`, so install both packages

对应源码：[chapter_intro_tirx/index.md:L12-L28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L12-L28)

这个提示框把本章示例的三项前提（Blackwell GPU、TIRx 编译器、CUDA 版 PyTorch）和安装命令、验证命令全部列出，并特别点出「通过 NVRTC 编译 CUDA 还需要 `cuda-bindings`」。末尾一句 "The runnable examples in later chapters use the same environment" 说明：**这套环境一次装好，全书通用**。

#### 4.1.4 代码实践

**实践目标**：在本地装好 TIRx 编译器并确认可以导入。

**操作步骤**：

```bash
# 1. 建议先进入独立虚拟环境（示例用 venv）
python -m venv mlsys-env
source mlsys-env/bin/activate

# 2. 安装（与书中命令完全一致）
pip install apache-tvm==0.26.0 cuda-bindings

# 3. 验证导入
python -c "import tvm, tvm.tirx; print(tvm.__version__)"
```

**需要观察的现象**：

- 第 3 步应打印一个版本号（预期为 `0.26.0`），且没有 `ImportError` / `ModuleNotFoundError`。
- 若 `import tvm.tirx` 报错而 `import tvm` 正常，大概率装到的 TVM 版本不对——回头检查是否精确安装了 `0.26.0`。

**预期结果**：终端输出 `0.26.0`。导入验证本身不依赖 GPU，在没有 GPU 的机器上通常也能通过（待本地验证）；但后续「编译并运行内核」需要 Blackwell GPU。

#### 4.1.5 小练习与答案

**练习 1**：为什么官方命令写 `apache-tvm==0.26.0` 而不是 `apache-tvm`？

**参考答案**：`==0.26.0` 把版本钉死。TIRx（`tvm.tirx` 模块）仍在快速演进，书中示例都基于 0.26.0 测试；不钉版本可能装到更新或更老的版本，API 与书中代码不一致，出现「照书写却跑不通」的问题。配套的 `tirx-kernels` 仓库也注明其固定 revision 是「与 Apache TVM 0.26.0 一起测试的版本」，见 4.4 节。

**练习 2**：删掉 `cuda-bindings` 只装 `apache-tvm`，会在哪个环节出问题？

**参考答案**：导入 `tvm.tirx` 大概率仍能成功，但在**编译内核**这一步会失败。TIRx 的 lowering 最后生成 CUDA 源码，由 TVM 通过 NVRTC 在运行时编译，而 Python 侧调用 NVRTC 依赖 `cuda-bindings` 提供的绑定（见 [chapter_intro_tirx/index.md:L15](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L15-L15) 的原话 "Compiling CUDA through NVRTC also requires `cuda-bindings`"）。

### 4.2 sm_100a 硬件要求：为什么必须是 Blackwell

#### 4.2.1 概念说明

书中所有内核都瞄准 Blackwell 架构标记 `sm_100a`（典型 GPU 为 B200）。原因在 u1-l1 建立的全书主线上：这本书把 Blackwell 硬件本身当作主角，内核里用到的 `tcgen05.mma`（第五代 Tensor Core 指令）、Tensor Memory（TMEM）、TMA 等机制都是这一代新增或大幅强化的，旧架构 GPU 上这些指令根本不存在。

因此「能不能跑书中内核」的判据不是显存大小或 CUDA 版本，而是：**GPU 架构是否为 `sm_100a`**。

配套要求还有一条容易被忽略：**PyTorch 必须是 CUDA 版**。PyTorch 在这里扮演两个角色——生成示例输入张量、计算参考答案做数值校验。CPU 版 PyTorch 无法把张量放到 GPU 上，验证回路就断了。

需要注意「Blackwell」不等于「任意 Blackwell 代号」。书中明确以 B200 这类数据中心 GPU（`sm_100a`）为例；消费级 Blackwell 显卡使用的是另一套架构标记（如 `sm_120`），并不包含 `tcgen05`/TMEM 这套指令与存储（此为本书之外的补充知识，请以 NVIDIA 官方文档为准，待确认）。

#### 4.2.2 核心流程

确认硬件资格的检查流程：

```text
1. nvidia-smi 查看显卡型号（是否为 B200 等 Blackwell 数据中心卡）
2. python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_capability())"
   → B200 预期输出类似 True (10, 0)
3. 同时确认 torch.cuda.is_available() 为 True（CUDA 版 PyTorch 装对了）
4. 三项都满足 → 可以编译运行书中内核
   任一不满足 → 后续实践改为「源码推演」模式（见 4.3.4 与第 5 节）
```

补充一个让检查自动化的小知识：编译时目标写 `"cuda"` 即可，TVM 会自动探测当前设备的架构（如 `sm_100a`），不需要手动写架构号——这正是下一模块编译代码里 `tvm.target.Target("cuda")` 的行为。

#### 4.2.3 源码精读

README 的 "Running the kernels" 一节开头就划定了硬件门槛：

> The kernels in this book target Blackwell (`sm_100a`), so running them needs a Blackwell GPU (such as a B200), the TIRx compiler, and a CUDA build of PyTorch.

对应源码：[README.md:L51-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L51-L54)

这一句把运行内核的三项前提一次性列全：Blackwell GPU（`sm_100a`，例如 B200）、TIRx 编译器、CUDA 版 PyTorch。紧接着的第 2 步说明 PyTorch 的用途：

对应源码：[README.md:L68-L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L68-L69)

原文说明 PyTorch 需要「与你 GPU 匹配的 CUDA 构建」，用途是「示例输入和参考校验」——正好对应我们说的两个角色。

「为什么旧 GPU 不行」的直接证据在第一个内核源码里。`hgemm_v1` 的 MMA 用 `dispatch="tcgen05"` 显式选择 Blackwell 的 `tcgen05.mma` 硬件路径：

对应源码：[chapter_intro_tirx/index.md:L143-L149](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L143-L149)

`Tx.gemm_async(..., dispatch="tcgen05", cta_group=1)` 这一行就是「必须有 sm_100a」的根源：它要求硬件提供第五代 Tensor Core 指令；同一份代码里的 `T.ptx.tcgen05.alloc`（分配 TMEM）也属于同类 Blackwell 专属指令。最后，编译目标只需写 `"cuda"`，TVM 会自动探测当前设备架构（原文 "TVM detects the current device architecture, such as `sm_100a`"），见 [chapter_intro_tirx/index.md:L177](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L177-L177)。

#### 4.2.4 代码实践

**实践目标**：判定本机能否运行书中内核，并留下书面记录。

**操作步骤**：

```bash
# 1. 查看显卡型号
nvidia-smi

# 2. 检查 CUDA 可用性与计算能力（先装好 CUDA 版 PyTorch）
python -c "import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_capability() if torch.cuda.is_available() else 'no cuda')"
```

**需要观察的现象**：

- 步骤 1 中显卡名称是否包含 B200/Blackwell 类型号；
- 步骤 2 第一行是否为 `True`（CUDA 版 PyTorch 且驱动可用），第二行的计算能力元组（B200 预期为 `(10, 0)`，待本地验证）。

**预期结果**：三项全部满足 → 环境合格；任一不满足 → 如实填写下面的限制清单，本手册后续的「运行型实践」自动降级为「源码推演型实践」：

| 检查项 | 命令 | 结果 | 是否满足 |
| --- | --- | --- | --- |
| TIRx 编译器 | `python -c "import tvm, tvm.tirx; print(tvm.__version__)"` | （填写） | ☐ |
| CUDA 版 PyTorch | `python -c "import torch; print(torch.cuda.is_available())"` | （填写） | ☐ |
| GPU 型号/架构 | `nvidia-smi` | （填写） | ☐ |
| 计算能力 | `torch.cuda.get_device_capability()` | （填写） | ☐ |

#### 4.2.5 小练习与答案

**练习 1**：一台装了 CUDA 12 与顶级 Ampere 显卡（`sm_80`）的机器，装好了 `apache-tvm==0.26.0` + `cuda-bindings` + CUDA 版 PyTorch，能跑书中内核吗？为什么？

**参考答案**：不能。工具链虽然齐全，但书中内核要求 `sm_100a`：内核里的 `dispatch="tcgen05"` 路径和 `T.ptx.tcgen05.*` 系列调用依赖 Blackwell 的第五代 Tensor Core 指令与 TMEM，`sm_80` 硬件上这些指令不存在。硬件门槛与工具链是相互独立的两回事。

**练习 2**：为什么验证回路里 PyTorch 必须是 CUDA 版，而不能用 CPU 版「先学着」？

**参考答案**：因为 PyTorch 张量同时承担「内核的输入/输出容器」和「参考答案计算器」两个角色。编译产物 `ex.mod(...)` 直接接受**位于 GPU 上**的 PyTorch 张量（见 4.3.3），CPU 版 PyTorch 无法创建 CUDA 张量，喂不进内核；参考校验 `A.float() @ B.float().T` 也需要与内核同一套张量交互。没有 CUDA 版 PyTorch，整条验证回路走不通。

**练习 3**：书中编译代码只写了 `tvm.target.Target("cuda")`，没有出现 `sm_100a` 字样，架构是在哪里确定的？

**参考答案**：由 TVM 在编译时自动探测当前设备架构得到（章节原文："The target can simply be `"cuda"`; TVM detects the current device architecture, such as `sm_100a`"，见 [chapter_intro_tirx/index.md:L177](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L177-L177)）。这也是「目标 GPU 资格检查」重要性的另一面：探测到的架构不是 `sm_100a` 时，后续基于 tcgen05 的 lowering 无法成立。

### 4.3 编译与验证回路：用 PyTorch 张量直接调用编译产物

#### 4.3.1 概念说明

环境就绪后，书中所有可运行示例都遵循同一个「编译—验证」回路。以第一个内核 `hgemm_v1`（计算 `D = A·Bᵀ` 的单 tile GEMM）为例，回路包含五步：

1. **构造内核**：调用 `hgemm_v1(M, N, K)` 得到一个 TIRx `PrimFunc`（函数体里用 `@T.prim_func` 装饰的内层函数）。
2. **编译**：把 `PrimFunc` 包进 `IRModule`，交给 `tvm.compile(..., tir_pipeline="tirx")`。`tir_pipeline="tirx"` 这个参数选择 TIRx 专属的 lowering 流水线，是其核心 pass `LowerTIRx` 的入口。
3. **准备数据**：用 `torch.randn` / `torch.zeros` 在 GPU 上创建 fp16 输入与输出张量。
4. **调用**：`ex.mod(A_tensor, B_tensor, D_tensor)`——编译产物**直接接受 PyTorch 张量**，无需手工转换成 TVM 自己的数据结构。
5. **校验**：用 PyTorch 算出参考答案 `D_ref`，与内核写入的 `D_tensor` 做 `torch.testing.assert_close` 断言，通过则打印 `PASS`。

这个套路之所以重要，是因为它就是全书每个内核的「单元测试」：后续 GEMM 九步优化、Flash Attention 4，每一步都以「跑通同一个断言」为正确性底线。

本模块还有一个**必须记住的书写规则**：TIRx 通过 Python 源码检视（source inspection）来解析内核，因此内核代码必须写在**文件或 notebook 单元格**里，不能放进 `python -c "..."`。直观理解：解析器需要读到内核函数的**源码文本**才能把它翻译成 IR，而 `python -c` 执行的代码不存在于任何文件中，无从检视。注意区分——本讲用来「验证安装」的 `python -c "import tvm, tvm.tirx; ..."` 没问题，因为它只做导入、不定义内核；一旦要定义 `@T.prim_func`，就必须落到文件。

#### 4.3.2 核心流程

```text
hgemm_v1.py（文件！）
    │  def hgemm_v1(M,N,K): 内部 @T.prim_func 定义 kernel
    ▼
kernel = hgemm_v1(128, 128, 64)          # 得到 PrimFunc
    ▼
tvm.compile(IRModule({"main": kernel}),
            target="cuda", tir_pipeline="tirx")   # LowerTIRx → CUDA
    ▼
ex.mod(A_t, B_t, D_t)                    # 直接传 PyTorch CUDA 张量
    ▼
D_ref = (A.float() @ B.float().T).half() # PyTorch 参考答案
assert_close(D_t, D_ref) → PASS          # 数值断言
```

关键认知：`ex.mod(...)` 的参数就是普通的 PyTorch CUDA 张量；内核计算结果直接写进输出张量 `D_t`，随后立刻可与 PyTorch 计算的参考值比较。

#### 4.3.3 源码精读

完整的编译与验证代码在 TIRx 入门章 "Compile and Verify the Result" 一节：

对应源码：[chapter_intro_tirx/index.md:L181-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L181-L205)

逐段说明这段代码做了什么：

- `target = tvm.target.Target("cuda")`：目标就是 `cuda`，架构由 TVM 自动探测（`sm_100a`）。
- `kernel = hgemm_v1(M, N, K)`：调用 4.2.3 里那个构造函数，拿到 `PrimFunc`。
- `ex = tvm.compile(tvm.IRModule({"main": kernel}), target=target, tir_pipeline="tirx")`：`PrimFunc` 先放进 `IRModule` 再编译；`tir_pipeline="tirx"` 启用 TIRx lowering 流水线。
- `A_tensor/B_tensor/D_tensor` 三个 `torch.*` 调用：在 `cuda` 设备上准备 fp16 输入与输出。
- `ex.mod(A_tensor, B_tensor, D_tensor)`：**直接传 PyTorch 张量**调用编译产物，无需任何手动转换。
- 最后四行：`(A.float() @ B.float().T).half()` 算参考答案，`assert_close(..., rtol=2e-2, atol=1e-2)` 断言，通过则打印 `PASS`。

「`ex.mod` 直接接受 PyTorch 张量」这一点在正文中被明确强调：

对应源码：[chapter_intro_tirx/index.md:L177-L180](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L177-L180)

原文写道 "The compiled `ex.mod(...)` accepts PyTorch tensors directly, so no manual conversion is needed"，并说明 `tir_pipeline="tirx"` 选择 TIRx lowering 流水线。

编译之后如果想「看看编译器做了什么」，书里给了三个检视调用：

对应源码：[chapter_intro_tirx/index.md:L243-L250](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L243-L250)

`kernel.show()` 与 `kernel.script()` 打印 lowering 前的 TIRx `PrimFunc`，`ex.mod.imports[0].inspect_source()` 打印最终生成的 CUDA C 源码——这是「源码推演型实践」的主力工具（无 GPU 也能用它研究 tile 操作如何变成线程级代码）。

最后是那条书写规则，README 在安装步骤之后用一句话交代：

> TIRx parses kernel source via Python source inspection, so examples should live in a file or notebook cell rather than inside `python -c`.

对应源码：[README.md:L83-L84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L83-L84)

这句话是所有书上示例的组织方式的注脚：内核定义永远以 `.py` 文件（或 notebook 单元格）为载体。

#### 4.3.4 代码实践

**实践目标**：把书上的第一个内核变成一个可反复运行的本地文件，走通完整验证回路（有 GPU），或产出一份观察笔记（无 GPU）。

**操作步骤**（以下均为示例代码，除引用的书中代码外需自行建立文件）：

1. 新建目录与文件 `kernels/hgemm_v1.py`，把 [chapter_intro_tirx/index.md:L72-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L72-L171) 中的 import 块与 `hgemm_v1` 函数**原样**抄入（注意：必须是文件，不能图省事用 `python -c`）。
2. 新建 `run_hgemm.py`，内容为 [chapter_intro_tirx/index.md:L181-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L181-L205) 的验证代码，开头加一行 `from kernels.hgemm_v1 import hgemm_v1`。
3. 运行 `python run_hgemm.py`。

**需要观察的现象**：

- 有 Blackwell GPU：终端先打印 `Max error vs torch reference: ...`（一个小量级数字），随后打印 `PASS`。
- 无 GPU：记录失败发生在哪一步（导入？`tvm.target.Target("cuda")`？`torch.cuda` 相关调用？），这就是你的环境断点。

**预期结果**：有 GPU 时输出 `PASS`（待本地验证——本讲义写作环境无 Blackwell GPU，未实际运行）。无 GPU 时完成下面的推演任务代替运行：

- 在 `run_hgemm.py` 中把编译与调用部分替换为 `kernel.show()`、`print(kernel.script())`，观察 lowering 之前的 TIRx IR 长什么样，写 3～5 行笔记描述你看到的 `Tx.cta.copy` / `Tx.gemm_async` / `Tx.wg.copy_async` 在 IR 中的形态（能否走到这一步取决于环境，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：把 `hgemm_v1` 的定义塞进 `python -c "def hgemm_v1(...): ..."` 里执行，会发生什么？为什么？

**参考答案**：会在 TIRx 解析内核时失败（无法得到可用的 `PrimFunc`）。因为 TIRx 依赖 Python 源码检视来解析内核——解析器需要读到函数的源码文本，而 `python -c` 执行的字符串不存在于任何文件中，无从检视。README 明确要求 "examples should live in a file or notebook cell rather than inside `python -c`"（[README.md:L83-L84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L83-L84)）。而 4.1 节的 `python -c "import tvm, tvm.tirx; ..."` 之所以合法，是因为它只导入模块、不定义内核。

**练习 2**：验证代码里参考答案为什么写成 `(A_tensor.float() @ B_tensor.float().T).half()`，而不是直接 `A_tensor @ B_tensor.T`？

**参考答案**：这是一条「高精度参考」套路：内核的累加器是 fp32（`hgemm_v1` 里 `acc_type = "float32"`），而输入输出是 fp16。参考计算先把输入提升到 fp32 再做矩阵乘、最后降回 fp16，尽量让参考值本身不引入额外舍入误差，这样 `D_tensor` 与 `D_ref` 的差异就主要来自内核行为，配合 `rtol=2e-2, atol=1e-2` 的宽容容差做断言。

**练习 3**：`tvm.compile(...)` 调用中，`tir_pipeline="tirx"` 这个参数去掉会怎样？

**参考答案**：会走默认的 TIR lowering 流水线而不是 TIRx 流水线。TIRx 流水线的核心 pass `LowerTIRx` 负责利用每个 tile 操作的 scope/layout/dispatch 信息选择具体实现，把 `Tx.gemm_async`、`Tx.cta.copy` 这类 tile 原语降成低层 TIR；不走这条流水线，这些 TIRx 专有的结构无法被正确处理（具体报错形态待本地验证）。

### 4.4 tirx-kernels 参考内核仓库

#### 4.4.1 概念说明

`tirx-kernels` 是同一组织（mlc-ai）下的**配套参考内核仓库**，在本书安装说明中定位为「可选的第 3 步」。它的价值在于：书中正文为了教学逐行展示的是简化/演进版本，而一个维护中的参考内核仓库可以当作成熟实现的对照。

安装它时有一个非常工程化的细节：**要 checkout 到一个固定的 commit**（`5be39749e7dfd2c4bdae9b4d396f8ec35af07126`）。README 的说法是「使用与 Apache TVM 0.26.0 一起测试的那个配套 revision」。这与 4.1 节钉死 `apache-tvm==0.26.0` 是同一个思想：**编译器与内核仓库必须版本配对**，否则 API 漂移会让你分不清报错来自自己的代码还是版本不匹配。

#### 4.4.2 核心流程

```text
1. git clone https://github.com/mlc-ai/tirx-kernels.git
2. cd tirx-kernels
3. git checkout 5be39749e7dfd2c4bdae9b4d396f8ec35af07126   # 钉死 revision
4. pip install -e .                                        # 可编辑安装
5. python -m tirx_kernels.test --kernel fp16_bf16_gemm     # 跑一个内核的测试
```

`pip install -e .` 的可编辑安装意味着后续如果你在该仓库里改内核源码，不需要重新安装即可生效——适合「抄着参考实现学」的用法。测试入口是 `python -m tirx_kernels.test`，用 `--kernel` 选择要测的内核（书中示例为 `fp16_bf16_gemm`）。

#### 4.4.3 源码精读

安装步骤的出处是 README 的第 3 步：

对应源码：[README.md:L71-L81](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L71-L81)

这段先说明「（可选）安装参考内核」，并强调「使用与 Apache TVM 0.26.0 配套测试的 revision」；随后给出四条命令（clone → checkout 固定 commit → `pip install -e .`），最后一句给出运行示例：`python -m tirx_kernels.test --kernel fp16_bf16_gemm`。

#### 4.4.4 代码实践

**实践目标**：安装参考内核仓库并跑通至少一个内核测试，确认「编译器 + 硬件 + 参考内核」三方版本配对无误。

**操作步骤**（有 Blackwell GPU 时）：

```bash
git clone https://github.com/mlc-ai/tirx-kernels.git
cd tirx-kernels
git checkout 5be39749e7dfd2c4bdae9b4d396f8ec35af07126
pip install -e .
python -m tirx_kernels.test --kernel fp16_bf16_gemm
```

**需要观察的现象**：测试命令执行后输出的通过/失败信息。

**预期结果**：`fp16_bf16_gemm` 内核测试通过（待本地验证）。若失败，优先排查三方版本：tvm 是否 `0.26.0`、tirx-kernels 是否在指定 commit、GPU 是否 `sm_100a`。

**无 GPU 替代实践**：在 GitHub 上打开 [tirx-kernels 仓库](https://github.com/mlc-ai/tirx-kernels)，确认该 commit 存在并浏览其目录结构，写一句话回答：「参考内核仓库与本书正文的章节（GEMM、Flash Attention）大致如何对应？」（此为源码阅读型实践；仓库内部结构以实际页面为准。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tirx-kernels` 要 checkout 固定 commit，而书却不锁定读者自己写的练习代码？

**参考答案**：因为编译器与内核之间存在 API 契约。书中指定 `apache-tvm==0.26.0`，`tirx-kernels` 主分支会随编译器演进不断变动，只有那个被注明「与 Apache TVM 0.26.0 一起测试」的 revision 才保证与读者装好的编译器配对。读者自己照书写的练习代码本来就以书中的版本为准，不存在配对问题。

**练习 2**：`pip install -e .` 与普通 `pip install .` 的区别在本场景下有什么意义？

**参考答案**：`-e` 是可编辑安装，包直接指向源码目录。学习本书时你可能会对照甚至修改 tirx-kernels 里的内核源码，可编辑安装让改动即时生效、无需反复重装，把参考仓库变成「可以动手做实验的教具」。

## 5. 综合实践

**任务：搭建你的「内核工作台」（kernel workbench）目录，一次搭好、全书复用。**

不管有没有 Blackwell GPU，都请完成以下目录（均为示例代码，需自行创建；`hgemm_v1` 部分照抄书中源码）：

```text
mlsys-workbench/
├── check_env.py        # 环境自检脚本
├── kernels/
│   └── hgemm_v1.py     # 从书中抄下的第一个内核（必须是文件！）
└── run_hgemm.py        # 编译 + PyTorch 验证回路
```

**第一步：写 `check_env.py`**，把 4.2.4 的检查项固化成脚本，输出四行结论（tvm 版本 / torch CUDA 可用性 / 显卡型号与计算能力 / 最终判定）。每次换机器、升级依赖后重跑一次。

**第二步：填 `kernels/hgemm_v1.py`**，原样抄录 [chapter_intro_tirx/index.md:L72-L171](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L72-L171) 的 import 与 `hgemm_v1` 定义。这一步刻意训练「内核必须落在文件里」的肌肉记忆。

**第三步：写 `run_hgemm.py`**，内容基于 [chapter_intro_tirx/index.md:L181-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L181-L205) 的验证代码，并做一处增强：在 `assert_close` 之前打印 `max_err`，在编译之后调用 `print(ex.mod.imports[0].inspect_source())` 把生成的 CUDA 源码存档到 `generated_cuda.txt`。

**验收标准**：

- 有 Blackwell GPU：`python run_hgemm.py` 打印出小的 `max_err` 与 `PASS`，且 `generated_cuda.txt` 里能看到 `tcgen05` 相关指令（待本地验证）。
- 无 GPU：`check_env.py` 如实输出不合格项，形成 4.2.4 表格的电子版存档；同时在笔记里写下「我的环境断点在第几步」。此后全书每个「运行型实践」都改用替代形态——用 `kernel.show()` / `kernel.script()` 检视 IR、用书中表格数据做计算推演、用 `img/scripts` 下的绘图脚本复现图表（这些不需要 GPU）。

这个工作台把本讲四个模块串在一起：apache-tvm 安装（4.1）决定 `check_env.py` 第一行能否通过；sm_100a 要求（4.2）决定验收走哪条分支；验证回路（4.3）就是 `run_hgemm.py` 本身；而如果你还装了 tirx-kernels（4.4），以后遇到书中简化实现看不懂的地方，就去参考仓库找成熟版本对照。

## 6. 本讲小结

- TIRx 不是独立包：它是 Apache TVM wheel 中的 `tvm.tirx` 模块，官方安装命令为 `pip install apache-tvm==0.26.0 cuda-bindings`，版本必须钉死。
- `cuda-bindings` 不可省略：TVM 生成的 CUDA 源码要经 NVRTC 运行时编译，Python 侧绑定由它提供。
- 运行书中内核的三项前提：Blackwell GPU（`sm_100a`，如 B200）+ TIRx 编译器 + CUDA 版 PyTorch；旧架构 GPU 缺少 `tcgen05`/TMEM 指令，装对工具链也跑不了。
- 全书统一的验证套路：`tvm.compile(..., tir_pipeline="tirx")` 编译，`ex.mod(...)` 直接接受 PyTorch CUDA 张量，与 `A.float() @ B.float().T` 的参考结果做 `assert_close` 断言。
- TIRx 通过 Python 源码检视解析内核：内核代码必须写在文件或 notebook 单元格中，`python -c` 只能用于导入验证这类不定义内核的命令。
- `tirx-kernels` 是可选的参考内核仓库，安装时 checkout 到与 TVM 0.26.0 配对测试的固定 revision，用 `python -m tirx_kernels.test --kernel fp16_bf16_gemm` 验证。

## 7. 下一步学习建议

环境课到此结束。按学习路线，下一讲是 **u2-l1「线程执行层级：thread 到 cluster」**——进入 Part I，从 GPU 的六级执行层级与 SIMT 模型开始建立硬件直觉；这是理解后续一切 scope 概念的地基。

如果你想先「眼见为实」地看一眼内核长什么样，可以在进入 u2 之前通读 [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) 全章（不必看懂每行），重点关注 `hgemm_v1` 的四个阶段与 scope/layout/dispatch 三要素的提法——它们会在单元九（u9）正式展开。另外，无论有无 GPU，都建议把第 5 节的工作台目录建好：有 GPU 的读者会在 u9-l2 用它跑出第一个 `PASS`，无 GPU 的读者也会用 `check_env.py` 的结论决定后续实践的形态。
