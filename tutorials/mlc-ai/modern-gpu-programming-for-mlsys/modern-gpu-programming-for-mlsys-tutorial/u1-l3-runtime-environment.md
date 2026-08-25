# 第 3 讲：运行环境与内核运行方式

## 1. 本讲目标

学完本讲，你应该能够：

1. 正确安装并验证 TIRx 编译器——它不是独立的包，而是 Apache TVM wheel 中的 `tvm.tirx` 模块，且必须与 `cuda-bindings` 配套安装。
2. 判断手头的 GPU 是否满足书中内核的 `sm_100a`（Blackwell）要求，理解为什么其他 GPU 装得上工具链也跑不了内核。
3. 掌握贯穿全书的内核运行套路：`tvm.compile(tir_pipeline="tirx")` 编译，用 PyTorch 张量直接调用编译产物，再与 PyTorch 参考结果做数值断言。
4. 记住一条第一天就可能踩坑的规则：TIRx 通过 Python 源码检视（source inspection）解析内核，因此内核代码必须写在文件或 notebook 单元格里，不能塞进 `python -c "..."`。
5. 了解可选的 `tirx-kernels` 参考内核仓库，以及官方把它的 revision 钉死的原因。

本讲是「环境课」：它不教你写内核，但之后每一讲的可运行实践都建立在本讲搭好的环境之上。没有 Blackwell GPU 的读者同样要读完本讲——本讲会给出明确的「环境限制清单」学习路径，后续讲义的实践都会提供源码推演型替代方案。

## 2. 前置知识

本讲需要的背景知识都很轻量，逐个用通俗语言过一遍：

- **pip 与虚拟环境**：本书工具链全部通过 `pip install` 获取。建议在干净的虚拟环境（`venv` 或 `conda`）中操作，避免与系统里已有的 PyTorch/TVM 版本互相污染。
- **编译器 wheel 是什么**：Apache TVM 是一个编译器项目，它的 Python 包（`apache-tvm`）里同时打包了 Python 前端和编译器后端的动态库。我们说的 TIRx 只是这个包里的一个模块（`tvm.tirx`），不需要、也没有一个单独叫 "tirx" 的包要装。
- **NVRTC**：NVIDIA Runtime Compilation，即在运行时把 CUDA C 源码编译成可加载的设备代码。TIRx 内核编译的最后一站是 CUDA C 源码，TVM 通过 NVRTC 完成这步；而 Python 侧调用 NVRTC 需要额外的绑定库——这就是 `cuda-bindings` 存在的原因。
- **GPU 架构标记（`sm_xx`）**：NVIDIA 每代 GPU 有一个架构标记，例如 Hopper 是 `sm_90`，Blackwell 数据中心 GPU 是 `sm_100a`（后缀 `a` 表示启用该架构的专属指令集）。书中内核大量使用 `tcgen05.mma`、Tensor Memory（TMEM）等 Blackwell 专属硬件特性，所以旧架构 GPU 即使装好了全部软件，也无法运行这些内核。
- **PyTorch 张量的最少知识**：书中的输入数据和参考答案都用 PyTorch 张量表示。本讲只会用到 `torch.randn`（随机输入）、`torch.zeros`（清零输出）、矩阵乘 `@` 和 `torch.testing.assert_close`（数值断言）。
- **与前一讲的衔接**：u1-l2 讲过，本地构建书站**不需要** GPU、也不需要 tvm——那只是 Sphinx 渲染 Markdown。本讲要装的是另一套东西：**运行书中内核**的环境。两件事容易混淆，请分开对待。

## 3. 本讲源码地图

本讲的关键文件只有两个主文件，外加三个用于交叉印证的文件：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md) | 仓库首页。其中 "Running the kernels" 一节（L51-L84）是官方的运行环境安装说明：TIRx 编译器、CUDA 版 PyTorch、可选的 tirx-kernels 三步，以及「示例必须写在文件里」的规则。 |
| [chapter_intro_tirx/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md) | 第一个 TIRx 内核 `hgemm_v1` 所在章节。章首 "Running the examples" 提示框解释了 `cuda-bindings` 的必要性；章节中段给出完整的「编译 + PyTorch 验证」代码，是本讲第 3 个模块的主要依据。 |
| [appendix/debugging_warp_specialized.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md) | 异步内核调试附录。开头的 "Before Debugging the Kernel" 给出了官方的环境自检命令（打印 `tvm.__file__`、设备名与 compute capability），是本讲排障步骤的出处。 |
| [chapter_tensor_cores/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md) | Tensor Core 章节。其中对 TMEM 在 `sm_100a` 上的具体规格（128 Lane 行 × 512 Col 列）的描述，用来解释「为什么必须是 Blackwell」。 |
| [chapter_flash_attention/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md) | Flash Attention 4 章节。其代码节选注明来自 `tirx-kernels` 的固定 revision，说明这个参考内核仓库贯穿全书后半部分。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① apache-tvm 安装；② sm_100a 硬件要求；③ 内核运行方式（编译与 PyTorch 验证回路）；④ tirx-kernels 参考内核。前两个模块对应规格中的「apache-tvm 安装」与「sm_100a 硬件要求」，后两个模块对应「tirx-kernels 参考内核」与学习目标中的验证套路。

### 4.1 apache-tvm 安装：TIRx 编译器从哪里来

#### 4.1.1 概念说明

第一个要纠正的直觉是「TIRx 是一个独立工具」——不对。**TIRx 以 `tvm.tirx` 模块的形式随 Apache TVM 的 wheel 一起发布**。也就是说，安装 TIRx 编译器 = 安装指定版本的 `apache-tvm`，没有第二个包要装。

第二个要点是**为什么还要装 `cuda-bindings`**。TIRx 内核编译的最后一站是 CUDA C 源码，TVM 通过 NVRTC 在运行时编译它；Python 侧调用 NVRTC 需要额外的绑定库，即 `cuda-bindings`。所以官方命令里这两个包总是一起出现，缺了后者，导入阶段不会报错，但走到 CUDA 编译时才会失败——这是很典型的「晚爆炸」依赖问题，不如一开始就装全。

第三个要点是**版本必须钉死**。官方命令使用 `apache-tvm==0.26.0` 这个精确版本号。TIRx 是快速演进中的新模块，不同 TVM 版本之间的 API 和行为可能有差异；跟随书中指定的版本，能最大程度避免「照着书敲却报错」。

第四个要点是**当心环境里有旧的 TVM**。如果机器上曾经从源码编译或克隆过 TVM，Python 可能导入的是那份旧代码而不是刚装的 wheel。调试附录给出的对策是同时打印 `tvm.__file__`（导入路径）和 `tvm.__version__`，先确认「用的确实是哪一份 TVM」，再谈其他问题。

#### 4.1.2 核心流程

安装与验证的完整流程：

```text
1. （建议）创建并激活一个干净的虚拟环境
2. pip install apache-tvm==0.26.0 cuda-bindings
3. 安装 CUDA 版 PyTorch（官方指引见 pytorch.org）
4. python -c "import tvm, tvm.tirx; print(tvm.__version__)"
   → 打印出版本号且无 ImportError，说明 TIRx 模块可用
5. （进阶自检）打印 tvm.__file__，确认导入的是 wheel 而非旧的环境残留
```

注意第 4 步的验证命令本身**可以**用 `python -c`，因为它只是导入模块、不定义内核——这与「内核代码不能放进 `python -c`」的规则不冲突，原因见 4.3.1。

#### 4.1.3 源码精读

**（1）官方安装命令出自 README 的 "Running the kernels" 一节。** [README.md:L51-L60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L51-L60) 说明：书中内核面向 Blackwell（`sm_100a`），运行需要 Blackwell GPU、TIRx 编译器和 CUDA 版 PyTorch；第 1 步安装的「TIRx 编译器」就是 Apache TVM wheel 里的 `tvm.tirx` 模块，命令为 `pip install apache-tvm==0.26.0 cuda-bindings`。

**（2）验证命令。** [README.md:L62-L66](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L62-L66) 给出验证方式：`python -c "import tvm, tvm.tirx; print(tvm.__version__)"`。这一行同时做了两件事：确认 `tvm` 能导入、确认其中的 `tvm.tirx` 子模块存在。任何一个失败都会抛 `ImportError`。

**（3）`cuda-bindings` 的必要性在章节里有明确解释。** [chapter_intro_tirx/index.md:L12-L25](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L12-L25) 的 "Running the examples" 提示框复述了安装与验证命令，并特别说明：TVM 通过 NVRTC 编译 CUDA 代码时还需要 `cuda-bindings`，所以两个包一起装。这个提示框结尾还有一句很实用的话——后续各章的可运行示例都用同一套环境，环境装好后不用反复折腾。

**（4）排障时先确认导入的是哪份 TVM。** [appendix/debugging_warp_specialized.md:L13-L17](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L13-L17) 给出的官方自检命令在版本号之外还打印了 `tvm.__file__`，并明确警告：如果 Python 导入的是一个旧的 TVM 检出（stale checkout），要先修环境、再改内核。本讲把它提前为安装后的例行检查。

#### 4.1.4 代码实践

**实践目标**：把 TIRx 编译器装进一个隔离环境，并完成两层验证（能导入 / 导入的是正确的那个包）。

**操作步骤**：

```bash
# 1. 创建并激活虚拟环境（conda 示例；venv 同理）
conda create -n tirx-book python=3.11 -y
conda activate tirx-book

# 2. 安装 TIRx 编译器与 NVRTC 绑定（版本按书中钉死）
pip install apache-tvm==0.26.0 cuda-bindings

# 3. 第一层验证：能导入、能打印版本
python -c "import tvm, tvm.tirx; print(tvm.__version__)"

# 4. 第二层验证：确认导入路径来自本环境的 site-packages
python -c "import tvm, tvm.tirx; print(tvm.__file__, tvm.__version__)"
```

**需要观察的现象**：

- 第 3 步应打印 `0.26.0`（待本地验证）。
- 第 4 步打印的 `tvm.__file__` 路径应位于当前虚拟环境的 `site-packages` 下；如果它指向某个源码目录（例如以前克隆的 TVM 仓库），说明环境被旧检出污染，需要先清理 `PYTHONPATH` 或调整环境变量。

**预期结果**：两条验证命令都成功执行；若第 3 步报 `ModuleNotFoundError: No module named 'tvm.tirx'`，最可能是装到了过旧的 `apache-tvm` 版本（TIRx 模块不存在），回到第 2 步核对版本号。本讲在撰写环境中未执行以上命令，具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：同事告诉你「我 `pip install tirx` 装上了 TIRx」，哪里不对？

**答案**：不存在名为 `tirx` 的独立安装包。TIRx 以 `tvm.tirx` 模块的形式随 Apache TVM 的 wheel（`apache-tvm`）发布，正确命令是 `pip install apache-tvm==0.26.0 cuda-bindings`。即使某个第三方包碰巧叫这个名字，也不是本书使用的 TIRx。

**练习 2**：为什么 `cuda-bindings` 缺失时，问题往往到很晚才暴露？

**答案**：`import tvm, tvm.tirx` 不需要 NVRTC，所以安装与导入验证都会通过；只有当 `tvm.compile` 走到「把生成的 CUDA C 源码经 NVRTC 编译成设备代码」这一步时才会失败。因此官方把两个包写在同一条安装命令里，一步装全。

**练习 3**：验证命令已经打印了正确的版本号，为什么调试附录还要建议打印 `tvm.__file__`？

**答案**：版本号只说明「导入的那份 TVM 版本正确」，不能说明「导入的是哪一份」。如果环境中残留旧的 TVM 源码检出，Python 可能优先导入它，其版本号也可能碰巧相同或相近；`tvm.__file__` 直接暴露实际导入路径，是排查这类污染最快的方式。

### 4.2 sm_100a 硬件要求：为什么必须是 Blackwell

#### 4.2.1 概念说明

[README.md:L51-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L51-L54) 把硬件门槛说得很直白：书中内核目标平台是 Blackwell（`sm_100a`，例如 B200），运行需要一块 Blackwell GPU。

为什么旧 GPU 不行？因为书中内核的每个环节几乎都建立在 **Blackwell 专属硬件**之上：

- 计算走 `tcgen05.mma`（第五代 Tensor Core 指令族），其累加器放在 Tensor Memory（TMEM）里——`sm_100a` 上每个 CTA 的 TMEM 是一块 128 Lane 行 × 512 Col 列、每格 32 位的专用片上存储，这代之前的 GPU 没有这个部件。
- 数据搬运依赖 TMA（Tensor Memory Accelerator）这样的异步搬运引擎。
- 后面的章节还会用到 cluster 内的 DSMEM、mbarrier 的字节追踪等机制。

工具链（tvm、PyTorch）在任何 CUDA 机器上都能装，但内核一旦被编译到 `tcgen05` 这条 dispatch 路径，旧硬件上就没有对应指令可以执行。**软件装好 ≠ 内核能跑**，这是本讲最想让你带走的一条判断。

另外要知道：**目标架构怎么指定**。书中示例把 target 写成 `"cuda"`，TVM 会自动检测当前设备的架构（例如 `sm_100a`）；在需要显式钉死架构的场合（如基准测试脚本），附录里出现了 `tvm.target.Target({"kind": "cuda", "arch": "sm_100a"})` 的写法。

#### 4.2.2 核心流程

判断「我能不能跑书中内核」的决策流程：

```text
1. 装好 4.1 的软件环境（任何机器都能做）
2. 检查 GPU：
   python -c "import torch; print(torch.cuda.get_device_name(), torch.cuda.get_device_capability())"
3. 判断：
   - 设备是 Blackwell 一代（README 举例 B200） → 可以编译并运行书中内核
   - 设备是其他架构（如 Hopper sm_90）        → 工具链可用，但内核不可运行
   - 无 GPU / 无 CUDA                         → 全部实践改为源码推演型
4. 若不可运行：写下环境限制清单（见 4.2.4），继续无 GPU 学习路径
```

#### 4.2.3 源码精读

**（1）硬件要求的原文。** [README.md:L51-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L51-L54)：小节标题即为 "Running the kernels (requires a Blackwell GPU)"，正文说明内核面向 Blackwell（`sm_100a`），需要 Blackwell GPU（如 B200）、TIRx 编译器和 CUDA 版 PyTorch——运行内核的三要素在这四行里凑齐了。

**（2）官方的设备检查命令。** [appendix/debugging_warp_specialized.md:L13-L15](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L13-L15) 用一行 `python -c "import torch; print(torch.cuda.get_device_name(), torch.cuda.get_device_capability())"` 同时拿到设备名和 compute capability；紧随其后的 [L17](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L17) 明确说：这些内核面向 Blackwell（`sm_100a`），如果 GPU 不是 Blackwell 一代，要先解决这个前提再谈内核。本讲把这个「先查环境再查代码」的原则提前到装机阶段。

**（3）「Blackwell 专属」的具体证据：TMEM 规格。** [chapter_tensor_cores/index.md:L104](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tensor_cores/index.md#L104) 描述：在 `sm_100a` 上，每个 CTA 的 TMEM 包含 128 个 Lane 行和 512 个 Col 列，每个坐标处是一个 32 位单元，`tcgen05.mma` 反复更新 TMEM 中的累加器。这段话同时给出了「书中内核依赖的硬件」和「它只存在于 Blackwell」两个事实，是硬件门槛的最佳注脚。

**（4）架构的自动检测与显式指定。** [chapter_intro_tirx/index.md:L177](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L177) 说明 target 写成 `"cuda"` 即可，TVM 会检测当前设备架构（如 `sm_100a`）；而基准测试附录的脚本里则出现了显式写法 [appendix/benchmarking_gpu_kernels.md:L1276](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L1276)：`tvm.target.Target({"kind": "cuda", "arch": "sm_100a"})`，用于把测量条件钉死。

#### 4.2.4 代码实践

**实践目标**：判定自己的机器属于哪一类（可运行 / 仅可编译推演 / 纯阅读），并产出一份「环境限制清单」。

**操作步骤**：

1. 在装好 4.1 环境的机器上执行：

   ```bash
   python -c "import torch; print(torch.cuda.get_device_name(), torch.cuda.get_device_capability())"
   ```

2. 记录打印的设备名与 compute capability，对照 README 的说明（Blackwell 一代、举例 B200）判断是否满足 `sm_100a`。具体的 capability 数字与架构对应表不在本书范围内，可查 NVIDIA 官方文档确认（待本地验证）。
3. 按判定结果写一份环境限制清单，模板如下（无 GPU 机器也要写）：

   | 检查项 | 结果 | 影响 |
   | --- | --- | --- |
   | `import tvm, tvm.tirx` | 成功 / 失败 | 失败则一切编译实践不可做 |
   | `torch.cuda.is_available()` | True / False | False 则不能运行内核与 GPU 验证 |
   | 设备名 / capability | （填写） | 决定内核是否可运行 |
   | 结论 | 可运行 / 仅推演 | 决定后续实践的形态 |

**需要观察的现象**：命令要么打印形如 `('NVIDIA B200', (x, y))` 的元组，要么因 `torch.cuda.is_available()` 为 False / 未装 CUDA 版 PyTorch 而报错。三种 outcome 分别对应流程图的三条分支。

**预期结果**：得到一份填好的限制清单。若结论是「仅推演」，本手册后续讲义的实践均提供源码阅读、伪代码推演、脚本复算等替代路径（例如 u2 的图表脚本、u3 的 roofline 计算），本清单就是你在各讲开头决定「走哪条路径」的依据。本讲未替你执行该命令，输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：一台 Hopper（`sm_90`）服务器装好了 `apache-tvm==0.26.0`、`cuda-bindings` 和 CUDA 版 PyTorch，验证命令也打印了 `0.26.0`。能运行书中的 `hgemm_v1` 吗？

**答案**：不能。验证命令只证明软件就绪；`hgemm_v1` 的 MMA 走 `dispatch="tcgen05"` 路径，累加器放在 TMEM 里，而 `tcgen05` 指令族与 TMEM 是 Blackwell（`sm_100a`）的硬件特性，Hopper 上不存在。硬件门槛由内核使用的指令集决定，与工具链是否装好无关。

**练习 2**：书中示例把 target 写成 `tvm.target.Target("cuda")`，为什么不写死 `sm_100a`？

**答案**：TVM 会从当前设备自动检测架构（如 `sm_100a`），写成 `"cuda"` 让同一份示例代码在正确硬件上「自动选对」目标，减少硬编码。反过来说，在基准测试这种要求条件完全可复现的场合，附录脚本就用 `{"kind": "cuda", "arch": "sm_100a"}` 显式钉死架构，避免检测环节引入波动。两种写法服务不同目的。

**练习 3**：调试附录为什么把「检查设备是不是 Blackwell」放在「读内核同步代码」之前？

**答案**：因为环境前提不满足时，任何内核侧的分析都没有意义——先运行环境自检（TVM 导入路径、版本、设备名与 capability），把环境与编译期问题排除后，再去怀疑内核本身。这是附录整篇调试方法论的第一步，也是本讲把它提前教的原因。

### 4.3 内核运行方式：编译与 PyTorch 验证回路

#### 4.3.1 概念说明

环境就绪后，书中所有内核都按同一个「运行回路」使用，这个回路在 `chapter_intro_tirx` 中第一次完整出现，之后各章反复复用：

1. **构造**：调用内核构建函数（如 `hgemm_v1(M, N, K)`）得到一个 TIRx `PrimFunc`。
2. **编译**：把 `PrimFunc` 放进 `IRModule`，用 `tvm.compile(..., tir_pipeline="tirx")` 触发 TIRx lowering pipeline；其中核心 pass 是 `LowerTIRx`，它依据每个 tile 操作的 scope/layout/dispatch 选择具体实现，把 `Tx.gemm_async`、`Tx.cta.copy` 之类的高层操作降成低层 TIR，后续 pass 再展平缓冲、分离主机/设备代码并生成设备代码。
3. **调用**：`ex.mod(...)` 直接接受 PyTorch 张量，无需手工转换格式。
4. **验证**：用 PyTorch 按相同数学定义算出参考结果，与内核输出做数值断言（`torch.testing.assert_close`），通过则打印 `PASS`。
5. **检视**（可选但强烈推荐）：`kernel.show()` / `kernel.script()` 打印 lowering 前的 `PrimFunc`；`ex.mod.imports[0].inspect_source()` 打印最终生成的 CUDA C 源码。对照两级代码可以看到一个 tile 操作到底变成了哪些底层指令。

这个回路里有一条特殊规则：**TIRx 通过 Python 源码检视解析内核**。内核在 Python 里是一个被 `@T.prim_func` 装饰的函数（见 `hgemm_v1` 内部的 `kernel`），TIRx 需要读到这个函数**真实的源代码文本**才能把它解析成 IR；`python -c "..."` 传入的字符串无法被源码检视机制可靠获取。所以：

- 内核定义必须写在 `.py` 文件或 notebook 单元格里；
- 而 `python -c "import tvm, tvm.tirx; ..."` 这类**不定义内核**的验证命令是安全的。

#### 4.3.2 核心流程

```text
hgemm_v1(M,N,K)          # 构造：返回 PrimFunc（需要真实源码文本）
        │
tvm.compile(IRModule({"main": kernel}),
            target="cuda", tir_pipeline="tirx")   # 编译：LowerTIRx → … → CUDA
        │
ex.mod(A_tensor, B_tensor, D_tensor)   # 调用：直接传 PyTorch 张量
        │
D_ref = (A.float() @ B.float().T).half()          # 参考：PyTorch 算同一数学定义
torch.testing.assert_close(D_tensor, D_ref, …)    # 断言：通过则 PASS
        │
kernel.show() / inspect_source()      # 检视：对比 lowering 前后两级代码
```

#### 4.3.3 源码精读

**（1）为什么需要源码检视：内核是 `@T.prim_func` 装饰的函数。** [chapter_intro_tirx/index.md:L85-L100](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L85-L100) 中，`hgemm_v1(M, N, K)` 是一个普通 Python 函数，内部用 `@T.prim_func` 装饰了 `kernel(A, B, D)`。TIRx 要把 `kernel` 的函数体解析成结构化 IR，依赖读取它的源码文本——这就是「示例必须写在文件或 notebook 单元格里」的根本原因。

**（2）规则的原文。** [README.md:L83-L84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L83-L84)：TIRx 通过 Python source inspection 解析内核源码，因此示例应放在文件或 notebook 单元格中，而不是 `python -c` 里。全书唯一的这个「格式级」硬约束，出自 README 的最后一段。

**（3）编译与验证的完整代码。** [chapter_intro_tirx/index.md:L181-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L181-L205) 是本模块的核心证据，逐段看：

- [L184-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L184-L190)：`target = tvm.target.Target("cuda")`；`kernel = hgemm_v1(M, N, K)` 构造内核；`ex = tvm.compile(tvm.IRModule({"main": kernel}), target=target, tir_pipeline="tirx")` 完成编译——`PrimFunc` 先放进 `IRModule`，`tir_pipeline="tirx"` 选择 TIRx 专用 lowering 管线。
- [L192-L198](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L192-L198)：先 `empty_cache` / `synchronize` 清理状态，用 `torch.randn` 造 fp16 输入 `A_tensor`、`B_tensor`，`torch.zeros` 造输出 `D_tensor`，然后 `ex.mod(A_tensor, B_tensor, D_tensor)` 一行完成调用——正如 [L179](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L179) 所说，编译产物直接接受 PyTorch 张量，无需手工转换。
- [L200-L204](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L200-L204)：参考结果 `D_ref = (A_tensor.float() @ B_tensor.float().T).half()`（先升 fp32 再算再降回 fp16，让参考更准），打印最大误差，`torch.testing.assert_close(D_tensor, D_ref, rtol=2e-2, atol=1e-2)` 断言，通过则打印 `PASS`。

**（4）两级代码检视。** [chapter_intro_tirx/index.md:L243-L250](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L243-L250)：`kernel.show()` 与 `kernel.script()` 打印 lowering 前的 TIRx `PrimFunc`，`ex.mod.imports[0].inspect_source()` 打印最终 CUDA C 源码。对照两级输出，可以看到 tile 操作生成了哪些底层指令、布局和线程 scope 如何变成具体的地址计算与控制流——这也是无 GPU 环境下最重要的学习手段之一。

#### 4.3.4 代码实践

**实践目标**：把「编译 + 验证」回路改造成一个可复用的脚本文件（而非 `python -c`），并为每行标注它在回路中的角色。

**操作步骤**：

1. 新建 `hgemm_run.py`，把 [chapter_intro_tirx/index.md:L181-L205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L181-L205) 的代码原样抄入，并在文件开头加上内核构建函数所需的 import（见 [L72-L78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L72-L78)）与 `hgemm_v1` 的定义（[L85-L170](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L85-L170)）。注意：正因为源码检视的要求，这一步必须落成文件，不能图省事塞进 `python -c`。
2. 在每行后面加注释，标注它属于回路的哪一步：`# 构造` / `# 编译` / `# 调用` / `# 参考` / `# 断言`。
3. 有 Blackwell GPU 时：`python hgemm_run.py`，期望末尾打印 `PASS`（待本地验证）。
4. 无 GPU 时：把 `tvm.compile` 及之后的行注释掉，只保留 `kernel = hgemm_v1(128, 128, 64)` 与 `kernel.show()`，运行观察打印出的 IR。构造与打印 `PrimFunc` 是否完全不触碰 GPU 驱动，待本地验证；若导入或构造阶段即失败，说明该环境连纯 IR 构造也不可用，实践退回「纯源码阅读」。

**需要观察的现象**：

- 有 GPU：终端先打印最大误差（形如 `Max error vs torch reference: 0.00xxxx`），随后 `PASS`。
- 无 GPU：`kernel.show()` 打印出的 IR 中应能看到 `Tx.cta.copy`、`Tx.gemm_async`、`Tx.wg.copy_async` 等 tile 操作名，与正文的描述一致。

**预期结果**：得到一份带注释的脚本 + 一张「回路角色」对照笔记。这张笔记会陪你走完整个 Part III：后续章节的内核换名字、换机制，回路本身不变。

#### 4.3.5 小练习与答案

**练习 1**：把验证命令 `python -c "import tvm, tvm.tirx; print(tvm.__version__)"` 和把 `hgemm_v1` 塞进 `python -c`，为什么前者合法、后者非法？

**答案**：前者只导入模块、不定义任何内核，不触发源码检视。后者需要 TIRx 解析 `@T.prim_func` 函数体的源代码文本，而 `python -c` 的字符串无法被源码检视机制可靠获取——README 明确要求示例放在文件或 notebook 单元格里。

**练习 2**：参考结果那一行为什么写成 `(A_tensor.float() @ B_tensor.float().T).half()`，而不是直接 `A_tensor @ B_tensor.T`？

**答案**：书中参考实现先把 fp16 输入升为 fp32、在 fp32 下做矩阵乘、再降回 fp16。fp16 直接累加的误差更大，用高精度参考能更公平地检验内核输出的正确性；最后的 `.half()` 保证与输出张量 `D_tensor` 同 dtype，才能通过 `assert_close` 比较。这是数值验证的常用手法：参考要「至少和被测对象一样准」。

**练习 3**：`kernel.show()` 和 `ex.mod.imports[0].inspect_source()` 各自展示什么？为什么说这一对输出对无 GPU 读者特别重要？

**答案**：前者打印 lowering 之前的 TIRx `PrimFunc`（tile 操作还是 `Tx.*` 形态）；后者打印 lowering 之后最终生成的 CUDA C 源码（tile 操作已展开为具体指令、地址计算与控制流）。对照两级代码可以观察「scope/layout/dispatch 三要素如何落地」。对无 GPU 读者，这是不运行内核也能研究编译器行为的主要窗口——生成源码的检视不需要执行内核（能否在无 GPU 环境完成编译本身，待本地验证）。

### 4.4 tirx-kernels 参考内核

#### 4.4.1 概念说明

`tirx-kernels` 是同组织（mlc-ai）下的配套仓库，收录了书中内核的完整参考实现。它在本手册中是**可选**依赖，但建议有 Blackwell GPU 的读者安装，原因有三：

1. **书里展示的是节选**。正文为了讲解只摘取内核的关键片段，完整可运行的版本（包括各种 shape、stage、phase 变量的定义）在 `tirx-kernels` 里。FA4 章开头的代码阅读约定就说明：本章代码节选自该仓库的 `flash_attention4.py`，并轻微缩略。
2. **它是现成的正确性基准**。仓库自带测试入口（`python -m tirx_kernels.test --kernel xxx`），装好后跑一条命令就能确认「环境 + 参考内核」整体工作正常，比先手写内核再排障要快得多。
3. **revision 被钉死**。README 指定 checkout 到 `5be39749e7dfd2c4bdae9b4d396f8ec35af07126`，并说明这是「与 Apache TVM 0.26.0 一起测试过的 companion revision」。与 4.1 钉死 `apache-tvm==0.26.0` 同理：内核 API 与编译器版本是配套演进的，两边都钉死才能复现书中的行为。

#### 4.4.2 核心流程

```text
（可选，建议有 Blackwell GPU 时执行）
1. git clone https://github.com/mlc-ai/tirx-kernels.git
2. cd tirx-kernels
3. git checkout 5be39749e7dfd2c4bdae9b4d396f8ec35af07126   # 钉死与 TVM 0.26.0 配套的 revision
4. pip install -e .
5. python -m tirx_kernels.test --kernel fp16_bf16_gemm     # 跑一个参考内核的自检
```

#### 4.4.3 源码精读

**（1）官方安装说明。** [README.md:L71-L79](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L71-L79)：可选项第 3 步——克隆 `tirx-kernels`、checkout 到指定 revision、`pip install -e .` 可编辑安装。注意措辞「Use the companion revision tested with Apache TVM 0.26.0」：revision 与编译器版本是成对给出的。

**（2）测试入口。** [README.md:L81](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L81)：以 `python -m tirx_kernels.test --kernel fp16_bf16_gemm` 为例运行。这个命令的作用是在你的机器上编译并运行参考 GEMM 内核、执行其正确性测试——它跑通了，说明 4.1/4.2/4.4 三层（编译器、硬件、参考内核）全部就绪。

**（3）这个仓库贯穿全书后半部分。** [chapter_flash_attention/index.md:L270](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L270) 交代了 FA4 章的代码出处：本章代码是从 `tirx-kernels`（同一 revision 链接）的 `flash_attention4.py` 中轻度缩略而来的节选，引用了在内核其他位置定义的 shape、stage 索引与 phase 变量。也就是说，读到 Part III/IV 时，这个仓库就是你的「完整版课本」。

#### 4.4.4 代码实践

**实践目标**：在有 Blackwell GPU 的机器上跑通一个官方参考内核的自检；无 GPU 时完成对应的源码阅读。

**操作步骤**：

- 路线 A（有 Blackwell GPU）：

  ```bash
  git clone https://github.com/mlc-ai/tirx-kernels.git
  cd tirx-kernels
  git checkout 5be39749e7dfd2c4bdae9b4d396f8ec35af07126
  pip install -e .
  python -m tirx_kernels.test --kernel fp16_bf16_gemm
  ```

  期望测试通过（具体输出形式待本地验证）。
- 路线 B（无 GPU）：不安装，改为打开 [chapter_flash_attention/index.md:L270](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L270) 中链接的该 revision 下 `tirx_kernels/attention/flash_attention4.py`，浏览其顶层结构，确认「正文节选 ↔ 完整实现」的对应关系，并在 4.2.4 的限制清单中追加一条「tirx-kernels 未安装，Part III/IV 实践采用源码阅读方式」。

**需要观察的现象**：路线 A 中测试命令会经历「编译参考内核 → 运行 → 数值校验」的过程，任何一环失败都会给出报错信息；路线 B 中应能在 `flash_attention4.py` 里找到正文提及的 `q_stage`、`acc_scale` 等名字。

**预期结果**：路线 A 通过则环境全链路就绪；路线 B 产出一条清单记录。两者都算完成本实践——本手册两条路径都认可。

#### 4.4.5 小练习与答案

**练习 1**：为什么 README 让你 `git checkout` 到一个具体 hash，而不是直接用 `tirx-kernels` 的 main 分支？

**答案**：main 分支会随上游持续演进，内核 API 与编译器版本一旦错位就可能「照书敲却跑不通」。钉死的 revision 是与 `apache-tvm==0.26.0` 成对测试过的快照，把「内核代码」和「编译器」两边的变量同时固定，才能复现书中行为。这与 4.1 钉死 TVM 版本是同一个思想：可复现性来自成对钉死。

**练习 2**：`python -m tirx_kernels.test --kernel fp16_bf16_gemm` 跑通意味着什么？没跑通又该如何定位？

**答案**：跑通意味着编译器、Blackwell GPU、cuda-bindings（NVRTC 路径）、参考内核四层全部正常，之后书中示例的失败基本可以归因于示例代码本身。没跑通则按层排查：先用 4.1 的命令确认 TVM 导入与版本，再用 4.2 的命令确认设备是 Blackwell——这正是调试附录「先环境、后内核」的第一步。

**练习 3**：FA4 章的代码节选与 `tirx-kernels` 里的完整实现是什么关系？

**答案**：正文为了讲解只摘取关键片段并做了轻度缩略，其中引用的 shape、stage 索引、phase 变量等在完整实现的内核其他位置定义。要运行或通读 FA4，需要安装（或对照阅读）README 钉死 revision 下的 `flash_attention4.py`。

## 5. 综合实践

**任务：制作一张「一键环境报告卡」。** 把本讲四个模块的检查合并成一个脚本 `env_report.py`，它不定义任何 TIRx 内核（因此形式上不受源码检视约束），逐项检查并在末尾给出结论。以下为示例代码：

```python
# 示例代码：环境报告卡（不定义 TIRx 内核，只做检查）
rows = []

def check(name, fn):
    try:
        rows.append((name, "OK", str(fn())))
    except Exception as e:
        rows.append((name, "FAIL", f"{type(e).__name__}: {e}"))

check("tvm/tirx 导入", lambda: __import__("tvm.tirx", fromlist=["tirx"]).__name__)
def _tvm_info():
    import tvm
    return f"{tvm.__version__} @ {tvm.__file__}"      # 版本 + 导入路径
check("TVM 版本与路径", _tvm_info)

def _torch_info():
    import torch
    if not torch.cuda.is_available():
        return "CUDA 不可用"
    return (f"{torch.version.cuda}, {torch.cuda.get_device_name()}, "
            f"capability={torch.cuda.get_device_capability()}")
check("PyTorch/CUDA", _torch_info)

for name, status, detail in rows:
    print(f"[{status:>4}] {name:16s} {detail}")

# 结论规则：
#   全 OK 且 capability 属 Blackwell 一代 → 可运行书中内核
#   tvm OK 但 CUDA 不可用               → 仅源码推演（对照 4.2.4 清单）
```

要求：

1. 运行脚本，把输出贴进自己的学习笔记；
2. 依据输出填写 4.2.4 的环境限制清单，明确宣告后续各讲实践走「可运行」还是「源码推演」路径；
3. 若报告显示 `tvm.__file__` 指向 wheel 之外的路径，先修复再继续；
4. 无 GPU 的读者额外做一步：从 4.3.4 的 `hgemm_run.py` 中摘出 `kernel.show()` 的输出（若环境允许），确认能在 IR 层面看到 `Tx.gemm_async`，作为后续「推演型实践」的起点。脚本中 capability 到架构的最终判定请对照 NVIDIA 官方文档（待本地验证）。

## 6. 本讲小结

- TIRx 不是独立包：它以 `tvm.tirx` 模块随 `apache-tvm==0.26.0` wheel 发布，且必须与 `cuda-bindings`（NVRTC 绑定）一起安装，验证命令之外还要用 `tvm.__file__` 确认导入路径无污染。
- 硬件门槛由指令集决定：书中内核依赖 `tcgen05.mma`、TMEM（`sm_100a` 上每 CTA 128 Lane × 512 Col）等 Blackwell 专属特性，非 Blackwell GPU 装好工具链也跑不了；target 写 `"cuda"` 时 TVM 自动检测架构。
- 内核运行回路五步走：构造 `PrimFunc` → `tvm.compile(..., tir_pipeline="tirx")` → `ex.mod` 直接收 PyTorch 张量 → `assert_close` 对比 fp32 参考断言 `PASS` → `kernel.show()` / `inspect_source()` 对照 lowering 前后两级代码。
- 一条格式硬约束：TIRx 依赖 Python 源码检视解析内核，内核代码必须写在文件或 notebook 单元格里；`python -c` 只能用于导入验证这类不定义内核的命令。
- `tirx-kernels` 是可选但推荐的参考内核仓库，须 checkout 到与 TVM 0.26.0 成对测试的 revision；FA4 等章节的正文代码即节选自该仓库。
- 没有 Blackwell GPU 不影响学习：产出环境限制清单后，后续各讲实践切换为源码推演、IR 检视与脚本复算路径。

## 7. 下一步学习建议

环境课到此结束。下一讲 **u2-l1《线程执行层级：thread 到 cluster》** 进入 Part I 的硬件部分：GPU 的六级执行层级（thread、warp、warpgroup、CTA、cluster、grid）与 SIMT 执行模型，并引出「操作 scope」概念——它是理解 TIRx 三要素之一 scope 的硬件基础。

建议带着本讲的结果进入下一讲：

- 有 Blackwell GPU 的读者：环境报告卡全绿，后续遇到内核示例可直接套用 4.3 的五步回路。
- 无 GPU 的读者：报告卡与限制清单就是你的「学习模式开关」，从 u2 起所有实践走推演路径；可以提前浏览 [chapter_intro_tirx/index.md:L209-L252](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L209-L252)（Scope、Layout、Dispatch 与编译小节），把本讲的「运行回路」与「三要素」概念先挂上钩，正式精读放在 u9。
