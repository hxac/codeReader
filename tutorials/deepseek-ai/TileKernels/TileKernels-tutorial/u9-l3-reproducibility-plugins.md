# 可复现性：随机种子与 pytest 插件机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「**seed = base + sha256(nodeid)**」这条公式是如何让**每个测试既稳定又互不干扰**的；
- 解释 pytest 的两条插件加载路径——`conftest.py` 自动发现与 `pytest_plugins` 显式注册——以及它们各自的触发时机；
- 说清 `tests/pytest_benchmark_plugin.py` 为什么**故意不命名为 `conftest.py`**，它规避的是 pluggy 的什么报错；
- 能够在本项目里新增或排查一个 pytest 插件，并理解随机种子在 `pytest -n 4`（xdist 并发）下依然可复现的原因。

本讲只读两个文件，但它们撑起了**整个测试套件的随机性控制**和**插件装配**两件基础设施。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个 pytest 的基础概念。如果你已经熟悉 pytest，可以跳到第 3 节。

- **测试函数（test item）与 nodeid**：pytest 把每个被收集到的测试看作一个「item」，并用一个全局唯一的字符串 `nodeid` 标识它。`nodeid` 通常长这样：

  ```
  tests/transpose/test_transpose.py::test_batched_transpose
  ```

  如果测试被 `@pytest.mark.parametrize` 参数化，`nodeid` 还会带上参数，例如：

  ```
  tests/transpose/test_transpose.py::test_batched_transpose[num_tokens=64-hidden=64-dtype=float16]
  ```

  关键点：**同一个测试在同一个项目里的 nodeid 是稳定不变的**，而**不同测试（含同一测试的不同参数化）的 nodeid 互不相同**。这正是本讲可复现性设计的基石。

- **fixture（夹具）**：pytest 的 fixture 是一种「测试前置准备」机制。用 `@pytest.fixture` 定义后，测试可以把它当参数请求；pytest 会在测试运行前自动执行 fixture、把返回值注入测试。带 `autouse=True` 的 fixture **不需要被请求**，会**自动**套用到当前作用域内的每一个测试上。

- **conftest.py**：pytest 约定，任何目录下的 `conftest.py` 都会被**自动发现并加载**，无需手动 import。里面定义的 fixture、hook 对该目录及其子目录的所有测试可见。它是「免注册」的全局配置点。

- **hook（钩子）与 pluggy**：pytest 的几乎所有行为都由 hook 驱动（如 `pytest_addoption` 注册命令行参数、`pytest_configure` 在配置阶段执行、`pytest_collection_modifyitems` 改写收集到的测试）。这些 hook 由底层的 **pluggy** 库管理：每个插件把自己的 hook 实现注册进 pluginmanager，pytest 在对应时机回调它们。

- **`pytest_plugins` 变量**：在 `conftest.py` 里写一个名为 `pytest_plugins` 的**字符串列表**，就是「显式注册插件」的标准入口。pytest 会按列表里的模块点路径去 import 这些模块，并把它们的 hook 注册进来。它与「conftest 自动发现」是**两条独立**的加载路径。

- **torch 的随机种子**：`torch.manual_seed(n)` 会设置 PyTorch **全局** CPU/CUDA 随机数生成器的种子。只要种子相同，`torch.randn` 等随机操作生成的张量就逐位相同。这是 GPU 算子对拍测试能稳定复现的前提。

如果你对上面这些概念还有点模糊，没关系，下面的源码会一步步把它们串起来。

## 3. 本讲源码地图

本讲只涉及两个文件，它们都位于 `tests/` 目录：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `tests/conftest.py` | 10 行 | 整个测试套件的「根 conftest」。本身**不定义任何 fixture/hook**，唯一职责是用 `pytest_plugins` 列表显式装配两个插件。 |
| `tests/pytest_random_plugin.py` | 18 行 | 随机种子插件。定义 `--seed` 命令行选项和一个 `autouse=True` 的 `seed` fixture，按 `base + sha256(nodeid)` 给每个测试派生并设置一个确定性种子。 |

另外，实践题会引用一个**本讲不精读、但必须理解其加载方式**的文件：

| 文件（仅引用） | 作用 |
| --- | --- |
| `tests/pytest_benchmark_plugin.py` | 性能基准插件（u9-l2 已详述）。它**故意不命名为 conftest.py**，本讲用它的命名来解释 pluggy 的重复注册问题。 |

这三个文件的关系可以用一句话概括：`conftest.py` 是「装配清单」，`pytest_random_plugin.py` 与 `pytest_benchmark_plugin.py` 是「清单上挂载的两个零件」。

## 4. 核心概念与源码讲解

本讲按两个最小模块展开：

- **模块一（conftest）**：插件加载机制——conftest 自动发现 vs `pytest_plugins` 显式注册，以及为什么 benchmark 插件不能叫 `conftest.py`。
- **模块二（pytest_random_plugin）**：随机种子派生——`seed = base + sha256(nodeid)` 如何同时满足「稳定」与「互不干扰」。

### 4.1 模块一：conftest 与 pytest_plugins 的加载机制

#### 4.1.1 概念说明

pytest 加载一个「插件」（即一段带 hook 的 Python 模块）有两条**独立**的路径：

1. **conftest 自动发现路径**：只要某个文件名叫 `conftest.py` 且在测试目录树上，pytest 就会在收集阶段自动 import 它、把里面的 fixture 和 hook 注册进来。这是**隐式**的，不需要你写任何注册语句。
2. **`pytest_plugins` 显式注册路径**：在任意 `conftest.py` 里定义一个字符串列表 `pytest_plugins = ['模块点路径', ...]`，pytest 会把这些模块当作插件**显式 import 并注册**。这是**显式**的。

本项目的 `tests/conftest.py` 走的是第 2 条路：它自己**极薄**，一行 hook 都不写，只列清单。

#### 4.1.2 核心流程

整个装配过程的时序如下：

```
pytest 启动
  └─ 收集阶段：自动发现 tests/conftest.py（路径 1，隐式）
       └─ 读取其中的 pytest_plugins = [
              'tests.pytest_random_plugin',
              'tests.pytest_benchmark_plugin',
          ]
            └─ 显式 import 这两个模块（路径 2）
                 ├─ pytest_random_plugin 的 hook 被注册（pytest_addoption + seed fixture）
                 └─ pytest_benchmark_plugin 的 hook 被注册（pytest_addoption + 各基准 hook）
  └─ 运行阶段：每个测试运行前，autouse 的 seed fixture 自动执行 → torch.manual_seed(...)
```

要点是：`conftest.py` **自己没有 hook**，它只是「触发器」。真正干活的是被它挂载的两个插件模块。

#### 4.1.3 源码精读

先看「装配清单」本身：

[tests/conftest.py:7-10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/conftest.py#L7-L10) —— 根 conftest 的全部有效代码：一个 `pytest_plugins` 列表，按模块点路径挂载两个插件。注意它**没有**定义任何 `pytest_addoption` / fixture，纯装配。

文件顶部的注释直接点出了本讲的第二个核心问题：

[tests/conftest.py:1-5](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/conftest.py#L1-L5) —— 注释说明：被挂载的插件「deliberately NOT named conftest.py」（故意不命名为 conftest.py），目的是避开 pluggy 的重复注册错误。

再看被挂载的 benchmark 插件，它的模块 docstring 把同一件事说得更细：

[tests/pytest_benchmark_plugin.py:6-9](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L6-L9) —— 模块 docstring 明确：本文件「故意不命名成 `conftest.py`」，而是通过根 `conftest.py` 的 `pytest_plugins` 加载；非 conftest 的名字能避免 pluggy 的重复注册错误。

#### 4.1.4 代码实践

**实践目标**：理解「重复注册」到底会触发什么错误，亲手复现它。

**操作步骤**（这是一次「源码阅读 + 思想实验」型实践，因为真的改名会破坏测试套件，标注「待本地验证」处请谨慎）：

1. 现状理解：`pytest_benchmark_plugin.py` 定义了 `pytest_addoption`，注册了 `--run-benchmark`、`--benchmark-output` 等命令行参数：

   [tests/pytest_benchmark_plugin.py:33-56](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L33-L56) —— benchmark 插件的 `pytest_addoption`，注册四个 CLI 选项。

2. **思想实验（待本地验证）**：假设把 `tests/pytest_benchmark_plugin.py` **重命名为 `tests/conftest.py`**（与现有 `tests/conftest.py` 冲突，相当于把 benchmark 的全部 hook 塞进那个会被自动发现的 conftest，且仍被根 conftest 的 `pytest_plugins` 引用）。那么这个模块会**同时**走两条加载路径：
   - 路径 1：因为名字叫 `conftest.py`，pytest 自动发现并注册一次；
   - 路径 2：因为出现在 `pytest_plugins` 列表里，又被显式注册一次。

   同一个模块的同一批 hook 实现（尤其是 `pytest_addoption`）被 pluggy 注册两次，最直接的症状是 `pytest_addoption` 被调用两次、`argparse` 试图第二次添加 `--run-benchmark` 而抛出 **`argparse.ArgumentError: conflicting option string: --run-benchmark`**；在更一般的情况下，pluggy 会直接抛 **`ValueError: Plugin already registered`**。两者都是「重复注册」的不同表现形式。

3. **正确做法的收益**：给它一个非 conftest 的名字（`pytest_benchmark_plugin.py`），它就**只**会被 `pytest_plugins` 这一条路径加载，注册恰好一次，无冲突。

**需要观察的现象**：在不改名的现状下，`pytest --co -q tests/`（只收集、不运行）能正常通过；若真的做了第 2 步的重命名实验（待本地验证），则会在启动阶段就报上述冲突错误。

**预期结果**：现状下命令行选项被注册一次、可见一次；思想实验中应观察到 `conflicting option string` 或 `Plugin already registered` 报错。

#### 4.1.5 小练习与答案

**练习 1**：如果某个插件**既不叫 `conftest.py`，也没有被任何 `pytest_plugins` 引用**，它的 hook 会被加载吗？

**参考答案**：不会。它既不在 conftest 自动发现路径上（名字不对），也不在显式注册路径上（没被引用），pytest 根本不会 import 它，hook 自然不会注册。这就是为什么 `pytest_random_plugin` 必须出现在根 conftest 的 `pytest_plugins` 列表里。

**练习 2**：把根 `tests/conftest.py` 里那两个挂载项的顺序对调，会影响测试结果吗？

**参考答案**：不影响正确性。`pytest_plugins` 列表只决定「注册哪些插件」，不保证严格的注册顺序语义；两个插件的 hook（随机种子、基准）职责正交、互不依赖。极端情况下若两个插件注册了同名 hook 且依赖执行顺序，才会有影响——但本项目不存在这种情况。

### 4.2 模块二：随机种子派生（pytest_random_plugin）

#### 4.2.1 概念说明

GPU 算子测试天然依赖随机输入（`torch.randn(...)` 造数据）。如果随机种子不可控，一个测试这次跑过了、下次却因为不同的随机数据而失败，调试就会变成噩梦。本项目用一个**极简**的 18 行插件解决了这个问题，核心思想是：

> **用测试自己的 nodeid 当作它的「身份证」，把身份证哈希成一个整数，作为它的专属随机种子。**

这样每个测试都拿到一个**只属于自己的、且永远不变**的种子，从而同时满足两个看似矛盾的要求：

- **稳定（可复现）**：同一个测试的 nodeid 永远不变 → 哈希永远不变 → 种子永远不变；
- **互不干扰**：不同测试的 nodeid 互不相同 → 哈希（以极高概率）互不相同 → 各自的随机流互不重叠。

#### 4.2.2 核心流程

种子派生分四步，每一步都对应一个设计意图：

```
1. 读 base：从命令行 --seed 读一个「基准」（默认 0）
2. 算 node_hash：对当前测试的 nodeid 做 SHA-256，取整数后对 2^31 取模
3. 合成：seed = base + node_hash
4. 注入：torch.manual_seed(seed)，并返回 seed 供测试需要时查看
```

用公式表达（行内用 `\( \)`，独立公式用 `\[ \]`）：

种子哈希部分为

\[
\text{node\_hash} \;=\; \operatorname{int}_{16}\!\big(\operatorname{sha256}(\text{nodeid})\big) \;\bmod\; 2^{31}
\]

最终种子为

\[
\text{seed} \;=\; \text{base} \;+\; \text{node\_hash}
\]

这里几个设计选择值得点出：

- **为什么用 SHA-256**：它是一个**确定性的纯函数**（输入相同 → 输出必然相同），且**雪崩效应**极强（输入差一个字符，输出几乎完全不同）。前者保证「稳定」，后者保证「不同 nodeid 几乎不撞同一个哈希」，即「互不干扰」。
- **为什么对 \(2^{31}\) 取模**：把哈希压到一个非负 31 位整数范围内（\([0, 2^{31}-1]\)），给后续 `base + node_hash` 留出舒服的加法空间，也避免任何因种子过大带来的边界问题。`torch.manual_seed` 本身能接受更大的整数，这里的取模是**防御性的保守选择**。
- **为什么 `autouse=True`**：让**每一个**测试都自动获得确定性种子，无需测试作者显式请求 `seed` fixture。这是「装一次、全局生效」的设定。

#### 4.2.3 源码精读

整个插件只有 18 行，逐段看：

[tests/pytest_random_plugin.py:7-8](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_random_plugin.py#L7-L8) —— `pytest_addoption` 注册一个 `--seed` 命令行选项，类型 `int`，默认 `0`。这就是公式里的 `base`，用户可以用 `pytest --seed 123` 整体平移所有测试的种子（用于在出现随机性相关 bug 时换一组数据复现）。

[tests/pytest_random_plugin.py:10-18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_random_plugin.py#L10-L18) —— 核心：`autouse=True` 的 `seed` fixture。`base` 来自上一行的 `--seed`；`node_hash` 是 `int(sha256(nodeid.encode()).hexdigest(), 16) % (2**31)`；最终 `seed = base + node_hash`，调用 `torch.manual_seed(seed)` 注入全局 RNG，并 `return seed`。

逐字拆解 `node_hash` 那一行：

- `request.node.nodeid` —— 当前测试的唯一身份证字符串；
- `.encode()` —— 转成 bytes（`hashlib` 要求 bytes 输入）；
- `hashlib.sha256(...).hexdigest()` —— 64 位十六进制字符串；
- `int(..., 16)` —— 按 16 进制解析成大整数；
- `% (2**31)` —— 压到 31 位非负范围。

#### 4.2.4 代码实践

**实践目标**：用一段最小代码（示例代码，非项目原有代码）亲手验证「稳定」与「互不干扰」两个性质。

**操作步骤**：

1. 在任意能跑 Python（无需 GPU）的环境，执行下面这段示例代码：

   ```python
   # 示例代码：复现 seed = base + sha256(nodeid) % 2^31 的派生
   import hashlib

   def nodeid_to_seed(nodeid: str, base: int = 0) -> int:
       h = int(hashlib.sha256(nodeid.encode()).hexdigest(), 16) % (2**31)
       return base + h

   # 两个不同测试的 nodeid（模拟参数化）
   a = 'tests/transpose/test_transpose.py::test_batched_transpose[num_tokens=64-hidden=64]'
   b = 'tests/transpose/test_transpose.py::test_batched_transpose[num_tokens=128-hidden=64]'

   print(nodeid_to_seed(a))      # 第一次
   print(nodeid_to_seed(a))      # 第二次：应与第一次完全相同 → 稳定
   print(nodeid_to_seed(b))      # 不同 nodeid：应与 a 的结果不同 → 互不干扰
   ```

2. 在**项目根目录**下，实际查看一个真实测试的 nodeid 与种子。先把 `seed` fixture 的返回值打印出来（临时在某个测试里加一行 `print(seed)`，或在测试体内 `print(torch.initial_seed())`），然后运行：

   ```bash
   pytest tests/transpose/test_transpose.py -v
   ```

   把 pytest 输出里的 `nodeid`（`-v` 会显示）喂给上面的 `nodeid_to_seed`，与打印出的实际种子比对。

**需要观察的现象**：

- 同一个 nodeid 两次调用 `nodeid_to_seed`，结果**逐位相同**（稳定性）。
- 两个仅参数不同的 nodeid，结果**不同**（互不干扰）。
- 用 `pytest --seed 1000 ...` 重跑，种子相比默认 `--seed 0` 恰好**整体 +1000**（因为 `seed = base + node_hash`，base 的变化是线性叠加的）。

**预期结果**：上述三条全部成立。若第 2 步在只读环境无法运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `nodeid` 而不是「测试函数名」来派生种子？

**参考答案**：测试函数名不唯一——参数化测试的每一组参数都共用同一个函数名（如 `test_batched_transpose`），但它们的 nodeid 不同（带 `[num_tokens=...]` 后缀）。如果只用函数名，同一函数的各组参数会撞同一个种子、共用同一条随机流，互相干扰。nodeid 把参数也编进了身份证，所以**每组参数都拿到独立的种子**。

**练习 2**：在 `pytest -n 4`（4 个 xdist worker 并发）下，这套种子方案还稳定吗？

**参考答案**：稳定。xdist 把每个 worker 当独立进程，但每个测试的 nodeid 不变，`sha256(nodeid)` 是纯函数，所以无论测试被分到哪个 worker、以什么顺序执行，它的种子都只由 nodeid 决定，结果可复现。唯一例外是：测试**内部**显式依赖了跨测试的执行顺序或全局可变状态——但那本身就不是可复现的写法，与本插件无关。

**练习 3**：有些测试（如 `tests/moe/test_top2_sum_gate.py`）里写了 `torch.Generator(device='cuda').manual_seed(42)`。它会和本插件的 `seed` fixture 冲突吗？

**参考答案**：不会，二者作用域不同。本插件 `seed` fixture 调用的是 `torch.manual_seed(seed)`，设置的是**全局** RNG；而测试里显式构造的 `torch.Generator().manual_seed(42)` 是一个**局部、独立**的生成器，传入具体算子后只影响那次调用，不影响全局 RNG。这其实是「测试想要一组完全可控的固定数据」时对全局种子的有意覆盖，不是冲突。

## 5. 综合实践

把本讲的两条主线（插件装配 + 种子派生）串成一个端到端小任务。

**任务**：为本项目**新增一个最小的「健康检查」插件**，验证整套装配与可复现机制。

要求：

1. 新建文件 `tests/pytest_health_plugin.py`（**注意命名：不要叫 `conftest.py`**），内含：
   - 一个 `pytest_addoption`，注册 `--health` 开关（`action='store_true'`，默认 `False`）；
   - 一个 `autouse=True` 的 fixture `health_print`，仅当 `--health` 打开时，在每个测试开始前 `print(f'[health] running {request.node.nodeid}, seed={...}')`。种子可以直接复用本讲的派生公式，或请求已有的 `seed` fixture。
2. 在 `tests/conftest.py` 的 `pytest_plugins` 列表里追加 `'tests.pytest_health_plugin'`。
3. 运行：

   ```bash
   pytest tests/transpose/test_transpose.py --health -v
   ```

**验收点**（对应本讲两个核心结论）：

- 插件被**恰好加载一次**：`--health` 选项可用、不报 `conflicting option string` / `Plugin already registered`（证明「非 conftest 命名 + 仅经 `pytest_plugins` 注册」的正确性）。
- 每个测试打印出的 `seed` 与其 `nodeid` 一一对应，且**重复运行结果不变**（证明 `seed = base + sha256(nodeid)` 的稳定性）。
- 仅参数不同的两个测试打印出**不同**的 seed（证明互不干扰）。
- 若你的插件文件不小心命名成了 `conftest.py`（待本地验证），复现第 4.1.4 节描述的重复注册报错，再改回正确命名。

> 本任务只改 `tests/` 目录下的测试基础设施，**不触碰任何 `tile_kernels/` 源码**，符合「只读源码、只在 tutorial/ 与测试侧动手」的约束。如果你处于只读环境无法落盘，可只做设计与逐行推演，并标注「待本地验证」。

## 6. 本讲小结

- `tests/conftest.py` 是一根极薄的「装配清单」，自身不写 hook，只靠 `pytest_plugins` 列表显式挂载两个插件模块。
- pytest 有**两条独立**的插件加载路径：`conftest.py` 自动发现（隐式）与 `pytest_plugins` 显式注册（显式）。一个模块若同时走两条路径，会被 pluggy 注册两次。
- `tests/pytest_benchmark_plugin.py` **故意不命名为 conftest.py**，是为了只被 `pytest_plugins` 加载一次，避开 pluggy 的重复注册错误（典型症状是 `pytest_addoption` 重复添加 `--run-benchmark` 导致的 `conflicting option string`，或 `ValueError: Plugin already registered`）。
- 随机种子由 `seed = base + sha256(nodeid) % 2^31` 派生：`sha256` 的**确定性**保证稳定，其**雪崩效应**保证不同 nodeid（含同一测试的不同参数化）几乎不撞同一种子，从而互不干扰。
- `autouse=True` 的 `seed` fixture 让每个测试**自动**获得确定性种子并 `torch.manual_seed` 注入全局 RNG，对测试作者完全透明。
- 这套机制在 `pytest -n 4`（xdist 并发）下依然可复现，因为种子只由 nodeid 决定，与 worker 分配和执行顺序无关。

## 7. 下一步学习建议

本讲是测试设施单元（第 9 单元）的收尾。建议接下来：

- **横向串读**：回到 u9-l1（`generator` / `numeric`）与 u9-l2（`benchmark_timer` / `benchmark_record`），把「确定性输入 → 数值对拍 → 性能回归」整条测试流水线在脑中连成一张图，理解随机种子是这条流水线「可复现」的总开关。
- **向应用层延伸**：随机种子主要服务于算子的**正确性对拍**。若想看这些对拍如何钉死具体算子行为，可挑一个家族（如 u4 量化、u5 MoE）的 `tests/` 用例，跟踪它如何调用 `generate_*` 造数据、如何用 `assert_equal` / `calc_diff` 判等。
- **进阶阅读**：若你对 pluggy 本身感兴趣，可阅读 pluggy 的 `PluginManager.register` 文档，理解它如何用「插件名 + hookimpl 列表」去重，从而从机制层面彻底理解「重复注册」为什么会发生。这条线不依赖本项目，但能让你今后排查任何 pytest 插件问题都更有底气。
