# u1-l4 examples 示例体系与官方文档学习路径

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 examples 目录的四级分层（00_hello_world / 01_beginner / 02_intermediate / 03_advanced）外加 models 的组织方式，以及官方推荐的三阶段学习路径。
2. 独立运行 `basic_ops.py`、`elementwise_ops.py`、`reduce_ops.py` 等初级示例，包括「跑全部 / 跑单个 / 列出用例 / 切换 SIM 模式」四种运行方式。
3. 读懂任何一个示例脚本的通用三段式骨架：kernel 定义区 → 测试函数区 → main 注册表区。
4. 掌握「文档给方向、示例给模板、源码给真相」的自主学习方法：遇到新算子时知道去 docs、examples、`python/pypto/op` 三处交叉查证。

本讲是第一单元（初识 PyPTO）的收尾：u1-l2 装好了环境并跑通了 hello_world，u1-l3 画出了源码地图，本讲教你把仓库里现成的上百个示例变成自己的「可运行参考手册」。

## 2. 前置知识

本讲不需要新的框架知识，但会用到以下几个基础概念，先用通俗语言解释：

- **argparse（命令行参数解析）**：Python 标准库，让脚本支持 `python xx.py --list --run_mode sim` 这样的命令行选项。examples 里几乎每个脚本都用它实现「跑全部还是跑单个用例」。
- **注册表模式（registry pattern）**：脚本末尾用一个字典把「用例 ID → 测试函数」登记起来，main 函数根据命令行参数从字典里挑函数执行。这样新增一个示例只需要加一个函数加一行登记。
- **闭包（closure）**：在一个函数内部定义另一个函数，内层函数可以引用外层函数的变量。`reduce_ops.py` 用这个技巧在运行时「按需生成」jit 算子。
- **golden 对比验证**：示例先用 PyTorch 算出参考答案（golden 值），再运行 PyPTO 算子，最后用 `assert_allclose` / `torch.testing.assert_close` 比对两者。这是算子开发中最基本的正确性验证手段。
- **承接 u1-l2 的两个概念**：`RunMode.NPU`（真机执行）与 `RunMode.SIM`（主机侧仿真，无需真机）；`set_vec_tile_shapes` / `set_cube_tile_shapes`（声明向量/立方体计算的数据分块大小，即 Tile）。
- **承接 u1-l3 的地图**：examples 只是「用户层」，它调用的算子 API 定义在 `python/pypto/op/` 下，jit 装饰器定义在 `python/pypto/frontend/parser/entry.py`。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| [examples/README.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/README.md) | examples 总入口：分级说明、运行方式、三阶段学习路径 |
| [examples/01_beginner/README.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/README.md) | 初级示例的四个子类（basic / compute / tiling / transform）说明 |
| [examples/01_beginner/basic/basic_ops.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py) | 基础操作速览：add / erfc / matmul / sum / dynamic_add 五个算子，展示最标准的三段式脚本骨架 |
| [examples/01_beginner/compute/README.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/README.md) | compute 子目录说明：三类计算算子的覆盖范围与运行方法 |
| [examples/01_beginner/compute/elementwise_ops.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py) | 逐元素算子大全：24 个用例，展示「一个算子三种用法（基础/广播/标量）」和用例注册表 |
| [examples/01_beginner/compute/reduce_ops.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py) | 归约算子示例：sum / amax / amin / maximum / minimum，展示闭包工厂与按维度数自适应 tile |
| [examples/01_beginner/compute/matmul_ops.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/matmul_ops.py) | 矩阵乘法示例：基础/批量/广播/转置/带 Bias 等配置 |
| [examples/validate_examples.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/validate_examples.py) | 批量执行器：被 `build_ci.py --example` 调用，把全部示例当作回归测试跑一遍 |
| [docs/zh/tutorials/index.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/index.md) | 官方教程总目录（toctree），五大板块的入口 |
| [docs/zh/tutorials/development/index.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/index.md) | 「算子开发」板块目录，与初级示例内容高度对应 |

## 4. 核心概念与源码讲解

### 4.1 examples 三级示例体系与学习路径

#### 4.1.1 概念说明

PyPTO 把教学示例直接放在仓库里，并且**按学习曲线分级**，每一级对应一类读者：

| 层级 | 定位 | 内容 |
| --- | --- | --- |
| 00_hello_world | 入门 | 一个向量加法，走通「装饰 → 标注 → 写回」最小流程（u1-l2 已精读） |
| 01_beginner | 初级 | basic（基础操作）、compute（计算算子）、tiling（分块策略）、transform（形状变换） |
| 02_intermediate | 中级 | basic_nn（神经网络组件）、operators（组合算子）、controlflow（循环/条件/动态 shape） |
| 03_advanced | 高级 | advanced_nn（Attention 等）、patterns（Function 复用等模式）、cost_model、aclgraph |
| models | 模型 | 真实大模型（DeepSeek、GLM）算子实现，需真机运行 |

这套分级的意义在于：**你永远可以找到「比你当前水平高一级」的可运行代码**作为抄写模板，而不必从 API 文档的零散签名里拼凑用法。

#### 4.1.2 核心流程

官方 README 给出的学习路径分三个阶段：

1. 夯实基础：hello_world → 01_beginner 的 basic / tiling / compute / transform 四个子目录。
2. 进阶组件：02_intermediate 的 operators / basic_nn / controlflow。
3. 深度实践：03_advanced 的 advanced_nn / patterns，再到 models 下的 DeepSeek、GLM 算子。

运行示例的通用命令形态（来自总 README）：

```text
python3 examples/01_beginner/basic/basic_ops.py            # 跑全部（默认 NPU 模式）
python3 <脚本> <用例ID>                                     # 跑指定用例
python3 <脚本> --list                                      # 列出所有用例
python3 <脚本> --run_mode sim                              # 仿真（CPU）模式
```

#### 4.1.3 源码精读

分级与定位说明在 [examples/README.md:L7-L13](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/README.md#L7-L13)：这段定义了 00/01/02/03/models 五个层级各自的适用人群。

运行方式说明在 [examples/README.md:L44-L62](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/README.md#L44-L62)：给出了「跑全部 / 跑特定样例 / --list / --run_mode sim」四类命令。注意 L54 的示例写的是 `basic_ops.py matmul::test_matmul`——这个 `op::test_name` 风格的用例 ID 其实是 compute 目录脚本的格式；`basic_ops.py` 实际使用的是 `-t matmul` 参数（见 4.2.3）。这正说明：**README 描述的是通用约定，具体脚本的参数以源码里的 argparse 为准**。

三阶段学习路径在 [examples/README.md:L64-L83](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/README.md#L64-L83)。

真机运行前的环境配置在 [examples/README.md:L28-L42](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/README.md#L28-L42)：`source set_env.sh` 之后必须 `export TILE_FWK_DEVICE_ID=0` 指定设备号；models 目录的样例明确要求真机。

初级示例的四个子类划分在 [examples/01_beginner/README.md:L9-L27](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/README.md#L9-L27)：basic 覆盖 `pypto.tensor`、`pypto.view`、`pypto.assemble` 等核心特性；compute 是算子 API 大全；tiling 讲 `set_vec_tile_shapes` / `set_cube_tile_shapes`；transform 讲 `transpose` / `reshape` / `slice`。

compute 子目录三个文件的分工在 [examples/01_beginner/compute/README.md:L14-L16](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/README.md#L14-L16)：elementwise_ops.py 覆盖 abs/add/clip/div/exp/log/mul/neg/pow/rsqrt/sqrt/ceil/floor/trunc/round/sub；matmul_ops.py 覆盖各种矩阵乘法配置；reduce_ops.py 覆盖 amax/amin/maximum/minimum/sum。

#### 4.1.4 代码实践

1. **实践目标**：不看本讲正文，独立从 README 中提取 examples 的完整目录树和运行命令。
2. **操作步骤**：
   - 阅读上面引用的三个 README 章节；
   - 在仓库根目录执行 `ls examples/`、`ls examples/01_beginner/`、`ls examples/01_beginner/compute/`，对照 README 画出目录树；
   - 执行 `python3 examples/01_beginner/compute/elementwise_ops.py --list`（在未配置真机的机器上这一步不触发任何 kernel 编译，只是打印注册表，可安全运行）。
3. **需要观察的现象**：--list 输出的用例 ID 是否都是 `算子名::测试函数名` 的格式；目录树里每一级是否有配套 README.md。
4. **预期结果**：得到一张四级目录树，以及 elementwise_ops.py 的 25 个用例 ID 清单。`--list` 的具体输出格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果你想学习「手写 Softmax 算子」，应该去哪个目录找参考实现？

**答案**：`examples/02_intermediate/operators/softmax/`。依据是 [examples/02_intermediate/README.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/02_intermediate/README.md) 中「自定义算子 (operators)」一节明确写到 Softmax 展示了 exp 计算与跨维度 sum 归约的手动分步实现。

**练习 2**：README 总入口推荐的「第一阶段：夯实基础」包含哪几个目录？按什么顺序读？

**答案**：`00_hello_world/hello_world.py` → `01_beginner/basic` → `01_beginner/tiling` → `01_beginner/compute` → `01_beginner/transform`，见 [examples/README.md:L66-L71](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/README.md#L66-L71)。顺序的逻辑是：先走通最小流程，再学 tile 概念，然后才是具体算子 API。

**练习 3**：为什么 models 目录的样例「请在真实设备运行」？

**答案**：models 是真实大模型（LLM）的算子实现（README L42 的补充说明），规模大、依赖完整的多核调度与立方体单元路径，SIM 仿真模式无法覆盖其全部行为，因此要求真机。

### 4.2 示例脚本的统一骨架：basic_ops.py 精读

#### 4.2.1 概念说明

examples 里的脚本虽然多，但几乎都遵循同一个**三段式骨架**：

1. **kernel 定义区**（模块顶部）：用 `@pypto.frontend.jit` 装饰的一到多个算子函数，这是「PyPTO 代码」。
2. **测试函数区**（中部）：普通 Python 函数，负责构造 torch 输入、调用 kernel、与 torch 参考实现比对。
3. **main 注册表区**（底部）：`examples` 字典登记用例 + `argparse` 解析命令行 + 设备初始化 + 循环执行。

认出这个骨架后，任何一个新示例你都能在 30 秒内定位到「我想抄的部分」。

#### 4.2.2 核心流程

以 `basic_ops.py` 为例，脚本的执行流程是：

```text
python3 basic_ops.py [-m npu|sim] [-t 用例...]
   │
   ├─ import 阶段：5 个 @jit kernel 被装饰（此时还没编译，编译发生在首次调用）
   │
   └─ main()
        ├─ argparse 解析 -m/--run_mode 和 -t/--tests
        ├─ device_init(run_mode)
        │     ├─ "sim" → runtime_options["run_mode"] = SIM，device = "cpu"
        │     └─ "npu" → 检查 torch_npu → 读 TILE_FWK_DEVICE_ID → device = "npu:0"
        └─ 逐个执行选中的 test 函数
              └─ test 内：torch 造数据 → 调 kernel（首次触发 JIT 编译）→ assert_close 比对
```

一个值得注意的机制：模块顶部的 `runtime_options` 是一个**普通 dict**，装饰器拿到的是它的引用；`device_init` 在 kernel 真正被调用之前修改这个 dict 的 `run_mode` 键即可切换模式。框架侧的证据是 jit 的执行函数在**每次内核调用时**才读取该 dict 决定走 NPU 还是 SIM：[python/pypto/frontend/parser/entry.py:L636-L646](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/entry.py#L636-L646)（`_execute_kernel` 中根据 `self._runtime_options` 的 `run_mode` 分发到 `LaunchKernelTorch` 或 CPU 仿真路径）。

#### 4.2.3 源码精读

模块级共享的运行选项在 [examples/01_beginner/basic/basic_ops.py:L23-L33](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L23-L33)：`runtime_options = {"run_mode": pypto.RunMode.NPU}` 这个 dict 被所有 kernel 装饰器共享；`add_kernel` 用 `pypto.Tensor[[...], pypto.DT_FP16]` 标注参数、`set_vec_tile_shapes(32, 32)` 声明 tile、`out[:] = (a + b) * 2.0` 写回结果——这三行就是 u1-l2 总结的「算子三要素」。

golden 对比验证在 [examples/01_beginner/basic/basic_ops.py:L36-L44](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L36-L44)：torch.randn 造输入，调用 `add_kernel(a, b, out)` 后用 `torch.testing.assert_close` 与 `(a + b) * 2.0` 比对。注意 torch 张量可以直接传给 jit 算子，无需手工转换。

矩阵乘法 kernel 在 [examples/01_beginner/basic/basic_ops.py:L63-L70](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L63-L70)：与向量算子有两点不同——tile 用 `set_cube_tile_shapes([32, 32], [64, 64], [64, 64])` 分别描述两个输入和输出的分块；写回用 `out.move(pypto.matmul(a, b, a.dtype))` 而不是 `out[:] = ...`。`move` 的语义细节在 u2-l2 讲 matmul 算子时展开，这里先记住「矩阵乘的写回写法不一样」。

动态 shape 算子在 [examples/01_beginner/basic/basic_ops.py:L103-L129](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L103-L129)：`pypto.DYNAMIC` 标注动态维度、`pypto.loop` 生成循环、`pypto.view` 切出固定大小的逻辑块并自动跟踪边界块的有效区域、`pypto.assemble` 把结果拼回输出——u1-l1 提到的 View/Assemble 术语在这里第一次以代码形式出现。这段是 u2-l6 动态 shape 课程的预习材料。

模式切换与设备初始化在 [examples/01_beginner/basic/basic_ops.py:L147-L162](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L147-L162)：`device_init` 修改共享 dict 并返回 torch 设备字符串；npu 分支会检查 torch_npu 是否安装、从环境变量读设备号。

用例注册表与命令行在 [examples/01_beginner/basic/basic_ops.py:L165-L201](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L165-L201)：`examples` 字典登记 5 个用例，argparse 提供 `-m/--run_mode`（choices 为 npu/sim）和 `-t/--tests`（choices 为字典的键）。所以对 basic_ops.py 来说，跑单个用例的命令是 `python3 basic_ops.py -t matmul`，而不是总 README 里写的 `matmul::test_matmul` 风格；这个脚本也没有实现 `--list`。

还有一个细节：`test_dynamic_add` 在非 NPU 设备上只打印警告并跳过验证，见 [examples/01_beginner/basic/basic_ops.py:L141-L144](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L141-L144)。也就是说不是所有用例都支持 SIM 模式验证。

#### 4.2.4 代码实践

1. **实践目标**：跑通 basic_ops.py，并验证「三段式骨架」的判断。
2. **操作步骤**：
   - 无真机：`python3 examples/01_beginner/basic/basic_ops.py -m sim -t add -t sum`
   - 有真机：`export TILE_FWK_DEVICE_ID=0` 后 `python3 examples/01_beginner/basic/basic_ops.py -t matmul`
   - 打开脚本，用三个注释标记把文件切成 kernel 定义区 / 测试函数区 / main 注册表区。
3. **需要观察的现象**：首次调用每个 kernel 时会出现一段编译等待（JIT），第二次调用同一 kernel 则明显变快（框架有内核缓存）；SIM 模式下 dynamic_add 会打印「not supported in sim mode, skip verification」警告。
4. **预期结果**：add 与 sum 用例通过断言打印 `✓ Test completed successfully`。SIM 模式下 matmul（bf16 立方体路径）能否通过属于待本地验证项。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `device_init` 在 main 里修改 `runtime_options` 这个 dict 就能影响所有已装饰的 kernel？

**答案**：Python 中 dict 是按引用传递的。装饰器在 import 阶段把 dict 对象存进了 jit 包装器；框架执行内核时（`entry.py` 的 `_execute_kernel`）才读取其中的 `run_mode`，因此运行前修改 dict 内容即可生效。

**练习 2**：`basic_ops.py` 里 add 的断言（L44）和 dynamic_add 的断言（L141-L144）处理方式有何不同？为什么？

**答案**：`test_add` 无条件断言（SIM/NPU 都验证）；`test_dynamic_add` 只在 `npu` 出现在 device 字符串中时断言，否则打印警告跳过。因为 dynamic_add 依赖 `pypto.loop`/`pypto.view` 的动态路径，该路径在 SIM 模式下的行为未覆盖验证。

**练习 3**：想只跑 erfc 和 sum 两个用例，命令是什么？

**答案**：`python3 examples/01_beginner/basic/basic_ops.py -t erfc sum`（`-t/--tests` 是 `nargs="*"` 的多值参数，见 [basic_ops.py:L182-L189](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L182-L189)）。

### 4.3 compute 示例的用例注册表与运行模式嗅探：elementwise_ops.py

#### 4.3.1 概念说明

compute 目录的三个脚本（elementwise / matmul / reduce）是**算子 API 的可执行文档**：每个文件把一类算子的所有用法组织成统一的用例注册表，用例 ID 采用 `算子名::测试函数名` 的两级格式（如 `add::test_add_broadcast`）。

这类脚本要解决一个 Python 特有的时序问题：`@pypto.frontend.jit` 装饰器在 **import 阶段**就执行了，此时命令行参数还没被 argparse 解析，但装饰器需要的 `run_mode` 已经要写进 `runtime_options`。elementwise_ops.py 的解法是**提前嗅探**：import 时先手工扫一遍 `sys.argv`，把 `--run_mode sim` 抢出来，再让装饰器使用。

另外，这个文件展示了「一个算子三种用法」的教学组织：同一个算子按 基础用法 / 广播用法 / 标量用法 分别给出 kernel 和测试。

#### 4.3.2 核心流程

```text
python3 elementwise_ops.py add::test_add_scalar --run_mode sim
   │
   ├─ import 阶段：
   │    _peek_run_mode_from_argv() 扫 sys.argv → 得到 "sim"
   │    global_run_mode = RunMode.SIM
   │    所有 @jit 装饰器以 {"run_mode": SIM} 装饰（值在此刻固定）
   │
   └─ main()
        ├─ argparse：位置参数 example_id + --list + --run_mode
        ├─ 若 --list：打印注册表后退出
        ├─ 若给了 example_id：校验合法性，只运行该用例
        └─ 否则按字典序运行全部用例
```

注意与 basic_ops.py 的区别：basic_ops 把可变的 dict 共享给装饰器、事后修改；elementwise 把值在 import 时固化（`global_run_mode` 是标量，装饰后无法再改），所以必须提前嗅探。两种方案都能达到「一条命令切换 NPU/SIM」的效果。

#### 4.3.3 源码精读

运行模式嗅探在 [examples/01_beginner/compute/elementwise_ops.py:L33-L49](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L33-L49)：`_peek_run_mode_from_argv` 同时支持 `--run_mode sim`（两个参数）和 `--run_mode=sim`（等号连写）两种写法；随后 `global_run_mode` 被所有装饰器引用，docstring 明确说明了动机——「让模块级装饰器能提前拿到 run_mode」。

最简 kernel 与验证策略在 [examples/01_beginner/compute/elementwise_ops.py:L77-L98](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L77-L98)：`abs_kernel` 三行完成 tile 设置、算子调用、写回；`test_abs_basic` 中 `assert_allclose` 只在 `global_run_mode == RunMode.NPU` 时执行——**SIM 模式下只打印 Output/Expected 而不断言**，用 SIM 跑示例时要靠肉眼或另写脚本核对输出。

同一算子的三种用法在 [examples/01_beginner/compute/elementwise_ops.py:L109-L172](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L109-L172)：`test_add_basic`（同形张量相加）、`test_add_broadcast`（(2,2) 与 (2,) 广播相加）、`test_add_scalar`（张量加标量）。标量用法的关键是 kernel 签名里直接声明 Python 标量参数 `scalar: float`（L170），标量会作为编译期常量进入算子。

用例注册表在 [examples/01_beginner/compute/elementwise_ops.py:L978-L1104](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L978-L1104)：25 个用例按 `算子::测试` 为键登记，每项带 name/description/function 三个字段，这就是 `--list` 输出的数据来源。

--list 与用例校验在 [examples/01_beginner/compute/elementwise_ops.py:L1106-L1127](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L1106-L1127)：`--list` 打印注册表后 return；非法用例 ID 会打印全部合法值并以退出码 1 结束。

argparse 定义在 [examples/01_beginner/compute/elementwise_ops.py:L948-L973](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L948-L973)：位置参数 `example_id`（可省略）、`--list` 开关、`--run_mode`（默认 npu）。

顺带对比 matmul 的注册表风格：[examples/01_beginner/compute/matmul_ops.py:L77-L104](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/matmul_ops.py#L77-L104) 中 `matmul_kernel` 同样使用 `set_cube_tile_shapes([32, 32], [64, 64], [64, 64])` 并通过 `out[:] = pypto.matmul(a, b, pypto.DT_FP32)` 显式指定输出 dtype（与 basic_ops.py 的 `out.move(...)` 是两种可对照的写法）。

还有一个注解风格的差异值得留意：basic_ops.py 用 `pypto.Tensor[[...], pypto.DT_FP16]`，compute 系列用 `pypto.Tensor([], pypto.DT_FP32)`。两种写法的共同点是 shape 都由调用时传入的实际张量决定；两种标注的精确语义差异在 u2-l1（Tensor 对象）中确认。

#### 4.3.4 代码实践

1. **实践目标**：掌握 compute 系列脚本的用例选择机制，并验证「SIM 模式不执行断言」这一结论。
2. **操作步骤**：
   - `python3 examples/01_beginner/compute/elementwise_ops.py --list`，记录用例总数；
   - `python3 examples/01_beginner/compute/elementwise_ops.py add::test_add_scalar --run_mode sim`；
   - 打开脚本 L132-L133，确认 `assert_allclose` 外面的 `if global_run_mode == pypto.RunMode.NPU` 条件。
3. **需要观察的现象**：SIM 模式下终端会打印 `Output:` 和 `Expected:` 两行张量值但不会因断言失败而退出；对照打印值人工确认 `add_scalar` 结果是 `[3, 4, 5]`。
4. **预期结果**：--list 列出 25 个用例；单用例运行打印 Output `[3., 4., 5.]`、Expected `[3., 4., 5.]`。具体输出格式待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果不加 `--run_mode sim` 直接在无 NPU 的机器上运行 elementwise_ops.py 会发生什么？

**答案**：`_peek_run_mode_from_argv` 返回默认值 "npu"，装饰器按 NPU 模式装饰；main 中 `args.run_mode == "npu"` 分支会要求环境变量 `TILE_FWK_DEVICE_ID`（`get_device_id` 未取到时会提示设置并直接返回），后续还依赖 torch_npu。因此在无真机环境应显式加 `--run_mode sim`。

**练习 2**：用例 ID 为什么设计成 `add::test_add_broadcast` 两级格式，而不是直接用函数名？

**答案**：函数名在多个算子之间会重复（如 basic/broadcast/scalar 三类测试如果只叫 `test_basic` 就无法区分算子），两级格式把「算子」和「场景」编码进 ID，既便于 --list 浏览，也便于命令行精确选择。这是注册表模式中常见的命名约定。

**练习 3**：`add_scalar_kernel` 中的 `scalar` 参数和 Tensor 参数有什么本质区别？

**答案**：Tensor 参数在运行时可以变化（shape/dtype 参与编译产物选择），而 `scalar: float` 这类 Python 标量在编译时作为常量编进算子——示例用它演示了「标量直接进 kernel 签名」的用法（[elementwise_ops.py:L169-L172](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/elementwise_ops.py#L169-L172)）。标量与编译缓存的关系将在 u2-l6 动态 shape 一讲深入。

### 4.4 归约算子进阶：reduce_ops.py 的闭包工厂与动态 tile

#### 4.4.1 概念说明

reduce_ops.py 在骨架上与前两个脚本相同，但引入了一个新的组织技巧：**闭包工厂（closure factory）**。elementwise 系列的 kernel 是模块级定义的「静态」函数；而 `sum_op` / `amax_op` / `amin_op` 是普通 Python 函数，**每次调用时在函数体内部现场定义 jit kernel**，把 `dim`、`keepdim`、张量维数等上下文捕获为闭包变量。

这样做的动机：归约算子的行为由 `dim`/`keepdim` 参数决定，输出 shape 也随之变化（keepdim=True 时被归约的维度保留为 1，False 时该维度消失）。闭包工厂把「根据参数推导输出 shape + 生成对应 kernel + 分配输出 + 调用」打包成一行可复用的调用：`out = sum_op(a, dim=-1, keepdim=False)`。

第二个知识点是 **tile 个数必须匹配张量维数**：`tile_shapes = [8] * len(a.shape)` 生成的列表长度等于输入张量的秩，再通过 `set_vec_tile_shapes(*tile_shapes)` 解包传入——二维张量得到 `(8, 8)`，三维张量得到 `(8, 8, 8)`。这解释了 u1-l2 留下的问题：为什么不同示例里 `set_vec_tile_shapes` 的参数个数不一样。

#### 4.4.2 核心流程

`sum_op(a, dim, keepdim)` 的执行流程：

```text
输入 a (torch.Tensor)、dim、keepdim
   │
   ├─ 1. 推导输出 shape：
   │      keepdim=True  → a.shape 中 dim 维置 1
   │      keepdim=False → a.shape 中删去 dim 维
   │
   ├─ 2. 在函数内部定义 sum_kernel（@jit 装饰）：
   │      tile_shapes = [8] * len(a.shape)     # tile 个数 = 张量维数
   │      set_vec_tile_shapes(*tile_shapes)
   │      out[:] = pypto.sum(a, dim=dim, keepdim=keepdim)   # dim/keepdim 是闭包捕获的常量
   │
   ├─ 3. torch.empty 分配输出张量
   └─ 4. sum_kernel(a, out) → 返回 out
```

每次调用 `sum_op` 都会定义一个新的 jit 函数对象；框架内部有 JIT 缓存机制（[python/pypto/frontend/parser/entry.py:L620-L634](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/frontend/parser/entry.py#L620-L634) 中的 `_kernel_module_cache` 命中日志），同一算子反复调用是否会重复编译，待本地验证（这也是 u2-l6 的伏笔）。

#### 4.4.3 源码精读

闭包工厂 `sum_op` 全文在 [examples/01_beginner/compute/reduce_ops.py:L78-L99](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L78-L99)：L82-L89 按 keepdim 推导 out_shape；L91-L95 在函数内定义 `sum_kernel`，`tile_shapes = [8] * len(a.shape)` 与 `pypto.sum(a, dim=dim, keepdim=keepdim)` 是本模块的两个核心语句；L97-L98 分配输出并调用。

keepdim 两种取值的对照测试在 [examples/01_beginner/compute/reduce_ops.py:L102-L132](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L102-L132)：同一个 `[[1,2,3],[4,5,6]]` 输入，`keepdim=False` 期望 `[6, 15]`（降为一位），`keepdim=True` 期望 `[[6],[15]]`（保留维度）。这组断言是理解归约输出 shape 规则的最直接材料。

沿不同维度归约的测试在 [examples/01_beginner/compute/reduce_ops.py:L135-L178](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L135-L178)：对同一个 (2,3,4) 张量分别沿 dim=0/1/-1 归约，三个 expected 张量可以当习题答案手工累加验证。

同构的其余算子：`amax_op` 在 [examples/01_beginner/compute/reduce_ops.py:L184-L204](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L184-L204)，`amin_op` 在 [examples/01_beginner/compute/reduce_ops.py:L293-L313](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L293-L313)，结构与 `sum_op` 完全一致，只换了一行算子调用（`pypto.amax` / `pypto.amin`）。`maximum`/`minimum` 是双输入逐元素比较而非归约，kernel 直接定义在测试函数里（[reduce_ops.py:L408-L421](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L408-L421)）。

归约用例注册表在 [examples/01_beginner/compute/reduce_ops.py:L559-L600](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L559-L600)：8 个用例，覆盖 sum/amax/amin 的 basic 与 different_dimensions 场景以及 maximum/minimum。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：亲手修改一个归约算子的输入 shape 与 tile 设置，观察并记录行为变化。
2. **操作步骤**：
   - 先跑原版：`python3 examples/01_beginner/compute/reduce_ops.py sum::test_sum_basic --run_mode sim`；
   - 复制 `reduce_ops.py` 到你自己的工作目录（**不要改仓库内的示例文件**），在副本中做两组修改：
     - **改 shape**：把 `test_sum_basic` 里的输入从 `[[1,2,3],[4,5,6]]`（2×3）改为 `torch.arange(32).reshape(4, 8).float()`（4×8），同步手算新的 expected（每行求和：`[28, 92, 156, 220]`）；
     - **改 tile**：把 `sum_op` 中的 `tile_shapes = [8] * len(a.shape)` 改为 `[16, 16]`，再改为 `[8]`（个数少于维数）。
   - 分别运行三组并记录输出。
3. **需要观察的现象**：
   - 改 shape 后结果是否仍与 torch 参考一致（归约结果与 tile 无关，tile 只影响切分方式）；
   - tile 从 (8,8) 改为 (16,16) 后结果是否不变；
   - tile 个数与张量维数不一致（二维张量只给一个 8）时框架是否报错、报什么错。
4. **预期结果**：shape 与合法 tile 的修改不影响数值正确性（SIM 模式下需人工对照打印的 Output/Expected；NPU 模式下有 assert_allclose 自动判定）；tile 个数不匹配的预期是报错，具体报错信息待本地验证。另注意 tile 取值还需满足对齐约束（32 字节对齐 FAQ 在 u7-l4 调试课展开）。

#### 4.4.5 小练习与答案

**练习 1**：`amax_op` 与 `maximum_op` 都含「最大值」语义，它们有什么区别？

**答案**：`amax_op` 是**归约**：沿 dim 维把多个值缩成一个最大值，输出 shape 变小；`maximum_op` 是**逐元素比较**：两个同形张量逐位取大，输出 shape 不变。前者的 kernel 里调用 `pypto.amax(a, dim=..., keepdim=...)`，后者调用 `pypto.maximum(a, b)`（[reduce_ops.py:L413-L417](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L413-L417)）。

**练习 2**：为什么 `sum_op` 里 `set_vec_tile_shapes(*tile_shapes)` 要用 `*` 解包，而 basic_ops.py 里直接写 `set_vec_tile_shapes(32, 32)`？

**答案**：两者最终传参形式相同（逐个维度传一个数）。`sum_op` 的 tile 个数取决于输入张量的维数，运行时才能确定，所以先构造列表再解包；basic_ops 的输入固定是二维，直接写两个字面量即可。`*` 把 `[8, 8]` 展开成 `8, 8` 两个独立参数。

**练习 3**：输入 shape 为 `(2, 3, 4)` 的张量执行 `sum_op(a, dim=1, keepdim=True)`，输出 shape 是什么？

**答案**：`(2, 1, 4)`。keepdim=True 把 dim=1 那一维置为 1 而不是删除（[reduce_ops.py:L82-L86](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/compute/reduce_ops.py#L82-L86) 的 out_shape 推导逻辑）；若 keepdim=False 则是 `(2, 4)`。

### 4.5 官方教程体系与批量校验：docs/zh/tutorials 与 validate_examples.py

#### 4.5.1 概念说明

examples 之外，仓库还有一套成体系的官方教程：`docs/zh/tutorials/`。它和 examples 是**同一套知识的两种载体**——文档讲概念和约束，示例给可运行代码。两者配合的检索方式是本讲想教给你的「自主学习方法论」：

遇到一个不熟悉的算子或机制时，按三步走：

1. **查文档**：`docs/zh/tutorials/development/` 下按主题找（如 tensor_operation.md、tiling.md）；
2. **抄示例**：`examples/01_beginner/compute/` 下找同算子的用例，直接改；
3. **看源码**：`python/pypto/op/` 下找算子的定义文件（如 math.py、reduction.py、matmul.py），确认签名与默认值。

最后还有一层保障：examples 不只是教程，它同时是**回归测试资产**。`examples/validate_examples.py` 会被 `build_ci.py --example` 调用，把全部示例脚本当作测试批量执行——这意味着示例代码永远与当前框架行为保持一致（CI 会跑），抄示例是安全的。

#### 4.5.2 核心流程

官方教程的目录结构：

```text
docs/zh/tutorials/
├── introduction/          # 项目介绍、快速开始、编程范式（u1-l1 已读）
├── development/           # 算子开发：张量创建/张量操作/tiling/编译/循环/条件
├── debug/                 # 调试：debug.md、performance.md、precision.md、性能案例
├── network_integration/   # 整网集成：pytorch_integration.md
└── appendix/              # 附录：环境变量、FAQ、术语表、trouble_shooting
```

批量校验器的执行流程：

```text
build_ci.py --example
   └─ examples/validate_examples.py [-t 目标目录] [--run_mode sim|npu] [-d 设备号]
        ├─ collect_scripts：rglob 收集目录下所有 .py（排除自身和 __init__.py）
        ├─ has_run_mode：检查脚本文本里是否有 --run_mode 参数
        └─ 对每个脚本：subprocess 运行（支持则附加 --run_mode），记录 PASS/FAIL 与耗时
```

#### 4.5.3 源码精读

教程总目录在 [docs/zh/tutorials/index.md:L3-L12](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/index.md#L3-L12)：toctree 依次列出 introduction（3 篇）、development、debug、network_integration、appendix 五大板块——这就是官方认可的阅读顺序。

「算子开发」板块目录在 [docs/zh/tutorials/development/index.md:L7-L12](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/development/index.md#L7-L12)：tensor_creation → tensor_operation → tiling → compile → loops → conditions。对照 examples：tensor_creation 对应 basic、tensor_operation 对应 compute、tiling 对应 tiling 子目录、loops/conditions 对应 02_intermediate/controlflow。**文档目录和示例目录互为镜像**，这是本讲最重要的结构性结论。

批量执行器的定位在 [examples/validate_examples.py:L11-L17](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/validate_examples.py#L11-L17)：模块 docstring 说明它由 `build_ci.py --example` 调用，并给出单目录、SIM 模式、并行（-w 4）三种调用示例。

脚本收集逻辑在 [examples/validate_examples.py:L34-L47](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/validate_examples.py#L34-L47)：`_collect_from_dir` 用 `rglob("*.py")` 递归收集并排除 `validate_examples.py` 自身与 `__init__.py`，支持传入单个文件或目录。

运行模式探测与执行在 [examples/validate_examples.py:L50-L71](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/validate_examples.py#L50-L71)：`has_run_mode` 直接检查脚本文本中是否出现 `--run_mode` 字符串；`run_script` 用 subprocess 运行、设置 `TILE_FWK_DEVICE_ID` 环境变量、带超时，返回码非 0 记为 FAIL。

算子 API 源码层的落点：`python/pypto/op/` 下按类别组织，与 compute 示例一一对应——`math.py`（elementwise）、`reduction.py`（reduce）、`matmul.py`（matmul）等（目录清单见 [python/pypto/op/](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/op/__init__.py)）。第三步「看源码」时从这里进入。

#### 4.5.4 代码实践

1. **实践目标**：用官方工具批量验证一个示例目录，体验「示例即测试」。
2. **操作步骤**：
   - `python3 examples/validate_examples.py -t examples/01_beginner/compute --run_mode sim`
   - 观察每个脚本的 PASS/FAIL 与耗时输出；
   - 若单跑某个失败脚本，再用 4.3 的方法定位到具体用例。
3. **需要观察的现象**：日志按脚本粒度输出 `[PASS]/[FAIL]` 与秒数；FAIL 时会附最后几行 stderr。
4. **预期结果**：compute 目录三个脚本全部 PASS（SIM 模式）。部分依赖真机的用例在 SIM 下的行为待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：想查 `set_vec_tile_shapes` 的对齐约束，应该去教程的哪个板块？

**答案**：`docs/zh/tutorials/appendix/faq/`（FAQ 板块，含 tileshape-32byte-alignment 等 FAQ）以及 `development/tiling.md`（正面讲 tiling 规则）。appendix/env_vars 下还有各环境变量说明。

**练习 2**：为什么说「examples 是可以放心抄的」？给出一条结构性理由。

**答案**：examples 接入了 CI——`validate_examples.py` 由 `build_ci.py --example` 调用批量执行全部示例（[validate_examples.py:L11-L17](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/validate_examples.py#L11-L17)），框架行为变化导致示例失效时会被发现，因此示例与当前框架保持一致。

**练习 3**：`has_run_mode` 用「文本里是否含 `--run_mode` 字符串」判断，这种实现有什么优缺点？

**答案**：优点是零侵入、不需要 import 目标脚本（import 会触发模块级 @jit 装饰，可能有副作用）；缺点是脆弱——如果脚本用别名或变量拼出该参数名，或者仅在注释里出现该字符串，就会误判。这是工程上「约定优于配置」的典型折衷。

## 5. 综合实践

**任务：产出一份《PyPTO 初级示例导读报告》**，把本讲四块内容串起来。

1. **跑三个脚本**：按 4.2.4、4.3.4、4.4.4 的步骤分别运行 `basic_ops.py`（-t add）、`elementwise_ops.py`（任选一个 add 用例）、`reduce_ops.py`（sum::test_sum_basic），均用 `--run_mode sim` / `-m sim`。记录每个脚本的：命令、用例 ID 风格（`-t matmul` vs `op::test_name`）、验证方式（无条件断言 / 仅 NPU 断言 / 跳过验证）。
2. **做一次修改实验**：在 `reduce_ops.py` 的副本中完成 4.4.4 的 shape 与 tile 修改，三组结果各截取一份 Output/Expected 打印，并回答：tile 改变会不会影响数值结果？什么情况下会报错？
3. **画一张对照表**：把 `docs/zh/tutorials/development/` 的六篇文档（tensor_creation / tensor_operation / tiling / compile / loops / conditions）与 examples 下最对应的示例目录一一配对，形成「文档 ↔ 示例」双栏表。
4. **走一遍三步检索法**：任选一个你没用过的算子（如 `clip` 或 `amax`），走「查文档 → 抄示例 → 看 python/pypto/op 源码」三步，把三处获得的信息各记一条（文档讲的约束、示例给的默认参数、源码里的函数签名）。

完成标志：报告包含 3 条运行记录、3 组对照输出、1 张配对表、1 次完整的三步检索。

## 6. 本讲小结

- examples 按 00_hello_world → 01_beginner → 02_intermediate → 03_advanced 四级递进，外加 models 真机大模型算子；总 README 给出了三阶段学习路径，初级又分 basic / compute / tiling / transform 四类。
- 示例脚本遵循三段式骨架：模块级 @jit kernel 定义区 → torch golden 对比测试区 → main 的用例注册表 + argparse 区；认出骨架就能快速定位任何示例的关键代码。
- 两种运行模式切换方案：basic_ops.py 共享可变 dict、运行前修改；compute 系列在 import 阶段用 `_peek_run_mode_from_argv` 嗅探 sys.argv 提前固化。SIM 模式下多数 compute 用例只打印结果不执行断言。
- reduce_ops.py 的闭包工厂展示了「按需生成 jit kernel」的组织方式，其 `tile_shapes = [8] * len(a.shape)` 揭示了 tile 个数必须等于张量维数的约束。
- `docs/zh/tutorials/development` 与 examples 初级目录互为镜像；examples 通过 `validate_examples.py` 接入 CI，是可以放心抄写的「可执行文档」。
- 自主探索新算子的三步法：查 development 文档 → 抄 compute 示例 → 读 `python/pypto/op` 源码签名。

## 7. 下一步学习建议

本讲结束后你已完成第一单元（初识 PyPTO），第二单元将进入 **Python 前端编程基础**：

- **下一讲 u2-l1（Tensor 对象与张量创建）**：本讲多处出现的 `pypto.Tensor([], DT_FP32)` 与 `pypto.Tensor[[...], pypto.DT_FP16]` 两种标注的差异、Tensor 的构造与 `from_torch` 转换将在该讲确认，建议先读 `python/pypto/tensor.py` 与 `docs/zh/tutorials/development/tensor_creation.md`。
- **u2-l2（Tensor 操作与算子 API）**：把本讲跑过的 elementwise/reduce/matmul 示例与 `python/pypto/op/math.py`、`reduction.py`、`matmul.py` 的源码定义对照阅读，重点看 `out.move` 与 `out[:]` 两种写回的区别。
- **u2-l4（Tile 编程入门）**：本讲 4.4.4 实验中「改 tile 不改结果」的现象，其原理（tile 只决定切分方式、不改变语义）与 tile 对齐约束将在该讲系统展开，参考 `examples/01_beginner/tiling/tiling_config.py` 与 `docs/zh/tutorials/development/tiling.md`。
- 想提前看动态 shape 的读者，可回看 [basic_ops.py:L103-L129](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/01_beginner/basic/basic_ops.py#L103-L129) 的 `dynamic_add_kernel`，它是 u2-l6 的预习材料。
