# 贡献新算子与测试

## 1. 本讲目标

学完本讲，你应该能够：

- 在 `examples/` 下按项目约定新增一个可被自动发现的算子脚本，并打印出 `Kernel Output Match!`。
- 看懂 `examples/bench_test.sh` 的「自动发现 → 并行执行 → 正则判定通过/失败」三段式逻辑，知道它如何扫描、如何判定一个算子"过了"。
- 在 `testing/python/` 下按命名与 fixture 规范写一个 pytest 用例，并用 `low_priority` / `ci_skip` 标记控制它在 CI 中的执行时机。
- 把代码格式化（`format.sh`）与本地跑测串成一条完整的贡献流程，自信地提交一个 PR。

本讲是 u7 实战单元的收口，把前几讲学到的算子写法落到「工程化交付」这一层——你的算子不仅要能跑，还要能被 CI 自动验证、被别人复现。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自依赖讲义）：

- **u3-l5 Element-wise 与 T.Parallel**：会用 `T.alloc_ub` / `T.copy` / `T.Scope("V")` / `T.tile.*` 写一个 Vector 核上的逐元素算子。本讲的示例算子正是这一类。
- **u7-l1 FlashAttention 实现案例**：理解 Developer 模式下「单条 `T.Pipelined` + `pass_configs` 开关」与 Expert 模式手写同步的两种交付形态，以及「算子正确性靠 `torch.testing.assert_close` 对拍」这一约定。
- **u1-l5 JIT 即时编译与运行总流程**：知道 `@tilelang.jit` 装饰器会在首次调用时触发「捕获 → 编译 → 加载」，`func(a, b)` 直接得到 NPU 上的输出张量。

几个本讲会用到的通俗概念：

- **算子（operator / kernel）**：在本项目语境里，一个 `.py` 脚本通常就是一个算子的「可运行示例 + 正确性自检」二合一。它既是文档，也是测试。
- **对拍（differential testing）**：把你的算子输出与 PyTorch 参考实现（`torch.xxx`）逐元素比较，`rtol/atol` 在容差内即视为正确。这是本项目判定算子通过的标准做法。
- **marker（pytest 标记）**：给测试用例贴的标签，用来在 CI 里按事件类型决定「跑还是不跑」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `CONTRIBUTING.md` | 贡献流程总纲：报 bug、提 PR、仓库搭建、跑测试的入口文档。 |
| `examples/elementwise/elementwise_add.py` | 一个最小算子样例，演示 `@tilelang.jit` → 对拍 → 打印 `Kernel Output Match!` 的完整写法，是新算子的最佳模板。 |
| `examples/reduce/example_reduce_min.py` | 另一个样例，演示带 `argparse` 命令行参数与 `if __name__ == "__main__":` 守卫的脚本结构。 |
| `examples/bench_test.sh` | examples 测试入口：自动发现 `.py`/`.sh` 脚本、并行执行、用正则判定通过、最后再跑 pytest。 |
| `testing/python/` | pytest 用例目录，存放针对前端 API 的回归测试。 |
| `pyproject.toml` | 注册 pytest 的 `low_priority` / `ci_skip` 两个自定义 marker。 |
| `docs/pytest_marker_guide.md` | marker 使用指南，讲解标记如何控制 CI 执行策略。 |
| `docs/coverage_guide.md` | 覆盖率统计手册，讲解如何用 `--coverage` 采集 Python/C++ 覆盖率。 |
| `.github/workflows/ci_cd.yml` | CI 主流水线，按事件类型应用 marker 过滤。 |
| `format.sh` | 提交前格式化脚本：yapf + ruff + codespell + clang-format。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：算子脚本规范、bench_test.sh 自动发现、pytest 用例规范、marker 与 CI 策略、贡献流程与格式化。

### 4.1 examples/ 算子脚本规范

#### 4.1.1 概念说明

在 tilelang-ascend 里，「算子」不是孤立的函数，而是 `examples/` 下一个**自洽的 Python 脚本**：它声明 kernel、构造输入、调用、与 PyTorch 参考实现对拍，最后打印一行 `Kernel Output Match!`。这样一个脚本同时承担三个角色——**可运行示例（文档）、正确性自检（测试）、性能基线（benchmark）**。这是本项目最重要的工程约定之一，理解它就能解释为什么 `bench_test.sh` 只靠「正则匹配输出」就能判断成百上千个脚本是否通过。

目录组织上，每个算子家族一个子目录，脚本名以 `example_` 或算子语义为前缀（如 `examples/reduce/example_reduce_min.py`、`examples/elementwise/elementwise_add.py`）。脚本既可 `python xxx.py` 直接跑，也可被 `bench_test.sh` 批量调度。

#### 4.1.2 核心流程

一个标准算子脚本的结构如下：

```text
1. 导入 tilelang / tilelang.language as T / torch
2. （可选）argparse 定义命令行参数（--m、--n 等）
3. @tilelang.jit(out_idx=[...]) 装饰一个工厂函数，返回 @T.prim_func
4. 主流程：构造 NPU 输入张量 → func(a, b) → torch 参考实现 → assert_close 对拍
5. 全部通过后打印 print("Kernel Output Match!")
```

关键点有三：

- **`out_idx`**：声明 kernel 的哪些参数是「输出」。`out_idx=[-1]` 表示最后一个张量是输出，运行时由框架自动分配（见 u1-l4）；`out_idx=[1]` 表示第二个参数为输出。
- **对拍容差**：NPU 浮点与 PyTorch CPU 参考实现存在累积误差，故用 `rtol=1e-2, atol=1e-2`，不能用默认的严格相等。
- **`Kernel Output Match!`**：这行字面量不是随便写的，它正是 `bench_test.sh` 判定通过的"契约"（见 4.2.3）。改了它，脚本就会被误判为失败。

#### 4.1.3 源码精读

先看最小样例 `elementwise_add.py` 的装饰器与工厂函数：

[examples/elementwise/elementwise_add.py:18-19](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py#L18-L19) —— `@tilelang.jit(out_idx=[-1])` 把返回 `@T.prim_func` 的工厂函数 `vec_add` 包装成可调用 kernel，输出 C 由运行时自动分配（u1-l4 已讲过这条链路）。

再看脚本尾部「对拍 + 打印契约」的两行：

[examples/elementwise/elementwise_add.py:59-66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/elementwise/elementwise_add.py#L59-L66) —— `print("init successful!")` 表示输入构造成功，`torch.testing.assert_close(c, ref_c, rtol=1e-2, atol=1e-2)` 对拍，最后 `print("Kernel Output Match!")` 是 bench_test.sh 判定通过的契约字面量。

带 `argparse` 与 `__main__` 守卫的更规整写法见 reduce 样例：

[examples/reduce/example_reduce_min.py:7-8](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/reduce/example_reduce_min.py#L7-L8) —— `@tilelang.jit(out_idx=[1])`，第二个张量 `B` 为输出。

[examples/reduce/example_reduce_min.py:34-45](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/reduce/example_reduce_min.py#L34-L45) —— `if __name__ == "__main__":` 守卫内先 `tilelang.cache.clear_cache()`、解析参数、构造输入。把可运行逻辑放进 `__main__` 守卫的好处是：脚本被 import 时不会自动执行（便于被其他脚本复用 kernel 定义），只有直接 `python xxx.py` 时才跑自检。

[examples/reduce/example_reduce_min.py:54-59](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/reduce/example_reduce_min.py#L54-L59) —— `func(a)` 调用、`torch.min(a, dim=-1).values` 参考实现、对拍、打印契约。

> 提示：`elementwise_add.py` 没有用 `__main__` 守卫，而是顶层直接执行——这是更"脚本化"的写法，本项目两种都接受，但新增算子推荐用 reduce 那种带守卫 + argparse 的规整写法，便于 CI 传参。

#### 4.1.4 代码实践

1. **实践目标**：跑通一个现成算子，确认它的输出里有那行"契约"。
2. **操作步骤**：`cd examples/elementwise && python elementwise_add.py`（需 NPU 环境）。
3. **观察现象**：终端依次打印 `init successful!` 与 `Kernel Output Match!`。
4. **预期结果**：看到这两行即代表算子正确并通过。
5. 若无真实 NPU，可改用 PTO + camodel 仿真跑（见 u7-l5），或退化为「源码阅读型实践」：在 `elementwise_add.py` 里把 `Kernel Output Match!` 改成别的字符串，运行 `bash ../bench_test.sh --dirs elementwise`，观察它被 `[FAILED]`——以此体会这行字的契约作用。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `elementwise_add.py` 用 `rtol=1e-2, atol=1e-2` 而不是默认严格相等？
**参考答案**：NPU 上 fp16/float 的 DMA 搬运、Vector 指令、累加顺序与 PyTorch CPU 参考实现不同，存在累积与舍入误差，必须放宽容差否则会误报。

**练习 2**：`out_idx=[-1]` 与 `out_idx=[1]` 在运行时行为上有什么共同点？
**参考答案**：两者都声明"该参数是输出、由运行时自动分配"，调用者无需手动创建输出张量，`func(a, b)` 直接返回结果。差别仅在索引位置。

### 4.2 bench_test.sh 自动发现与运行

#### 4.2.1 概念说明

`examples/bench_test.sh` 是 examples 测试的**总入口**，也是 CI 调用的核心脚本。它要解决的问题是：`examples/` 下有几十个算子目录、上百个脚本，怎么用一套机制把它们全部自动找出来、跑一遍、报个通过率？答案是把"算子通过"这件事降维成"脚本输出里是否匹配某个正则"——这正是 4.1 说的 `Kernel Output Match!` 契约的下半场。理解这个脚本，你才知道新增算子要满足什么规范才能被 CI 认到。

#### 4.2.2 核心流程

bench_test.sh 由四段构成，伪代码如下：

```text
阶段 0  解析命令行参数（--dirs / --skip-pytest / --coverage / --pytest-markers 等）
阶段 1  收集脚本：collect_test_scripts(dir) 按规则扫描 .py / .sh，存入 all_scripts[]
阶段 2  并行执行：每个脚本后台跑，MAX_JOBS 控制并发；正则判定 PASS/FAIL
阶段 3  跑 pytest：在 testing/python/ 上跑 pytest（除非 --skip-pytest）
阶段 4  汇总：Bench 通过数 + Pytest 通过数 = 总通过率
```

**判定规则（最关键）**：脚本输出只要匹配以下任一正则即判 PASS——

- `Kernel Output Match`（大小写、空格不敏感）
- `Test Passed!`（大小写不敏感）
- 或该任务是自定义任务且退出码为 0

否则判 FAIL，并打印末 5 行辅助调试。

#### 4.2.3 源码精读

**参数解析**：脚本开头解析一组开关：

[examples/bench_test.sh:4-9](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/bench_test.sh#L4-L9) —— 关键开关有 `--skip-pytest`（只改了 examples 文件时跳过 pytest）、`--coverage` / `--enable-cpp-coverage`（采集覆盖率）、`--dirs`（增量测试，只跑指定目录）、`--pytest-markers`（透传 pytest marker 过滤）。

**并发与 autotuner 配置**：

[examples/bench_test.sh:84-86](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/bench_test.sh#L84-L86) —— `MAX_JOBS=8` 控制并行度（按 NPU 负载调整），同时设了两个 autotuner 的 CPU 并发环境变量（呼应 u7-l6）。

**自动发现函数 collect_test_scripts**——这是新增算子必须遵守的"被发现规则"：

[examples/bench_test.sh:152-198](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/bench_test.sh#L152-L198) —— 该函数对每个目录做三件事：

1. 特殊目录白名单：`./gemm_aot`、`./torch_tl_ascend`、`./dispatch_combine`、`./shmem` 只收集指定的 `.sh`，不扫 `.py`（L157-L167）。`./flash_attention` 只收主目录、排除 `fa_opt`。
2. 通用目录：用 `find -maxdepth 2 -name "*.py"` 扫描，但**排除** `__init__.py`、`*_golden.py`、`utils.py`、`sfa_golden.py`、`bench_sfa/` 子目录等辅助文件（L182-L190）。
3. 再扫 `run_*.sh` 与 `test_*.sh` 命名的 bash 脚本（L194）。

> 这意味着：你新增的算子脚本只要放在 `examples/<你的目录>/` 下、扩展名 `.py`、文件名不是 `__init__.py`/`*_golden.py`/`utils.py`，就会被自动发现。深度限制 `maxdepth 2`，所以别嵌套太深。

**全量扫描入口**：

[examples/bench_test.sh:262](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/bench_test.sh#L262) —— 全量模式下 `find . -maxdepth 1 -type d` 遍历每个一级目录（排除 `dispatch_combine`、`shmem`），对每个目录调 `collect_test_scripts`。

**逐脚本执行**：

[examples/bench_test.sh:337-352](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/bench_test.sh#L337-L352) —— `.py` 脚本 `cd` 到其所在目录后 `python <脚本名>` 执行（这正是为什么脚本里用相对路径或绝对 shape 即可）；带 `--coverage` 时改用 `coverage run`。`.sh` 脚本则 `bash` 执行。

**通过/失败判定（契约正则）**：

[examples/bench_test.sh:359-361](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/bench_test.sh#L359-L361) —— 这三行是整个 examples 测试体系的"判官"。注意正则大小写、空格都用字符类 `[Kk]` 写成不敏感，所以你打印 `kernel output match` 也能过——但**强烈建议严格按 `Kernel Output Match!` 打印**，保持全仓一致。

**并发控制**：

[examples/bench_test.sh:373-375](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/bench_test.sh#L373-L375) —— 后台 `&` 启动，`jobs -r -p | wc -l` 数在跑数，达 `MAX_JOBS` 就 `wait -n` 等任意一个结束再继续，实现有界并发。

**pytest 收尾**：

[examples/bench_test.sh:425](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/bench_test.sh#L425) —— examples 跑完后，用 `pytest --forked -v -n $MAX_JOJS testing/python/` 跑 pytest（`--forked` + `-n` 是 pytest-xdist 的进程级并行，因 tilelang 有全局缓存需进程隔离）。`--pytest-markers` 透传 marker 过滤（见 4.4）。

**最终汇总**：

[examples/bench_test.sh:511-523](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/bench_test.sh#L511-L523) —— 合并 Bench 与 Pytest 两段结果，`xfailed`（预期失败）计入 passed，输出总通过率。该格式供 CI workflow 解析。

#### 4.2.4 代码实践

1. **实践目标**：在不跑全量的情况下，验证 bench_test.sh 的"自动发现 + 正则判定"机制。
2. **操作步骤**：`cd examples && bash bench_test.sh --dirs elementwise --skip-pytest`。
3. **观察现象**：输出里应出现 `Collected scripts from elementwise: N files`、`[PASSED] ./elementwise/elementwise_add.py`，最后 `Execution Summary` 显示该目录的通过数。
4. **预期结果**：`elementwise_add.py` 被自动发现并 `[PASSED]`。
5. 进一步：临时把 `elementwise_add.py` 里的 `Kernel Output Match!` 注释掉再跑，应看到 `[FAILED] ... Last line: ...`，体会契约正则的作用。**待本地验证**（需 NPU）。

#### 4.2.5 小练习与答案

**练习 1**：你新增了 `examples/my_op/helper.py`（工具函数）和 `examples/my_op/example_my_op.py`（算子主体），bench_test.sh 会扫到哪个？
**参考答案**：只扫到 `example_my_op.py`。`helper.py` 虽扩展名 `.py` 会被 `find` 列出，但若它没有打印 `Kernel Output Match!` 就会被判 `[FAILED]`——所以辅助文件应命名为 `utils.py`（在排除名单里）或放进更深/带排除前缀的位置，避免被当成算子执行。

**练习 2**：为什么 pytest 段要用 `--forked`？
**参考答案**：tilelang 有进程级全局缓存（`tilelang.cache`），多个并行 worker 共享同一进程会互相污染；`--forked` 让每个测试在独立子进程跑，配合 `-n` 并发既快又隔离。

### 4.3 testing/python 的 pytest 用例规范

#### 4.3.1 概念说明

`testing/python/` 存放的是**前端 API 回归测试**，与 `examples/` 互补：examples 偏「可运行示例 + 端到端正确性」，pytest 偏「针对某个 API/参数组合的细粒度回归」。比如 `test_tilelang_ascend_language_elementwise.py` 把 elementwise 的每种算子（abs、add、乘）按 dtype × target 笛卡尔积展开成大量用例，确保前端语义在各组合下都不回归。

命名约定：文件名统一以 `test_tilelang_ascend_language_<主题>.py` 开头，pytest 默认会收集 `test_*.py`。子目录按主题分（如 `language/parallel/`、`language/cvseparate/`）。

#### 4.3.2 核心流程

一个典型 pytest 用例文件的结构：

```text
1. 导入 tilelang / T / torch / pytest
2. 定义 pass_configs 字典：开启一组自动 pass 开关（CV 分离、CV 同步、自动同步、内存规划）
3. @pytest.fixture(session, autouse=True) clear_cache：整轮测试前清缓存
4. @pytest.fixture setup_random_seed：固定随机种子保证可复现
5. 一个不含 @tilelang.jit 的纯工厂函数，返回 @T.prim_func（便于在多参数下复用）
6. @pytest.mark.parametrize 参数化的 test_xxx 入口，内部调用工厂 + 对拍
```

注意：pytest 用例里的"工厂函数"通常**不**加 `@tilelang.jit`，而是在测试里手动调 `tilelang.compile(prim_func, target=..., pass_configs=...)` 编译，这样能在参数化里灵活切换 target（ascendc / pto）与配置。

#### 4.3.3 源码精读

以 elementwise 测试套件为例。先看它统一的 pass_configs 与两个 fixture：

[testing/python/language/test_tilelang_ascend_language_elementwise.py:17-22](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_elementwise.py#L17-L22) —— `pass_configs` 一次性开启四个自动开关：`TL_ASCEND_AUTO_CV_COMBINE`（自动 CV 分离，u5-l1）、`TL_ASCEND_AUTO_CV_SYNC`（自动核间同步，u5-l1）、`TL_ASCEND_AUTO_SYNC`（自动核内同步，u4-l3）、`TL_ASCEND_MEMORY_PLANNING`（自动缓冲复用，u6-l5）。这正是 Developer 模式"省心写法"的标准配置（呼应 u7-l1）。

[testing/python/language/test_tilelang_ascend_language_elementwise.py:25-35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_elementwise.py#L25-L35) —— `clear_cache` fixture（session 级、autouse，整轮自动生效一次）保证缓存干净；`setup_random_seed` 固定 `torch.manual_seed(0)` 保证可复现。这是几乎所有测试文件都复用的两个 fixture。

再看一个纯工厂函数（不带 `@tilelang.jit`）：

[testing/python/language/test_tilelang_ascend_language_elementwise.py:57-79](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_elementwise.py#L57-L79) —— `vec_abs(M, N, ...)` 返回一个 `@T.prim_func`，供参数化的 test 函数按需编译。这种"工厂返回 prim_func"的写法是 testing 目录的主流范式，与 examples 里"工厂加 @tilelang.jit"的写法不同。

> 术语提示：`@pytest.fixture(scope="session", autouse=True)` 中，`scope="session"` 表示整个测试会话只执行一次，`autouse=True` 表示自动应用到所有测试无需显式声明参数。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：看懂一个 pytest 用例如何把"一个工厂函数"展开成"多 target × 多 shape"的测试矩阵。
2. **操作步骤**：`grep -n "parametrize\|def test_" testing/python/language/test_tilelang_ascend_language_elementwise.py | head -20`，找到某个 `test_xxx` 与它的 `@pytest.mark.parametrize` 装饰器，数一下它会产生多少个用例。
3. **观察现象**：每个 parametrize 维度做笛卡尔积，比如 `target ∈ {ascendc, pto}` × `shape ∈ {...}` → N 个用例。
4. **预期结果**：能说出该 test 函数在 pytest 里展开后的用例 ID 形如 `test_xxx[ascendc-1024]`、`test_xxx[pto-1024]`。
5. 这类"读参数化矩阵"是写新测试前必做的功课。**待本地验证**（命令本身一定可跑，结果依文件而定）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 testing 里的工厂函数不加 `@tilelang.jit`？
**参考答案**：`@tilelang.jit` 会在首次调用时按当时 shape/target 触发编译并缓存，不利于在参数化里灵活切换 target 与配置；测试倾向手动 `tilelang.compile(prim_func, target=..., pass_configs=...)` 精确控制每一次编译，便于覆盖 ascendc/pto 双后端。

**练习 2**：`clear_cache` fixture 为何用 `scope="session"` 而非默认的 `function`？
**参考答案**：每个测试函数都清缓存会非常慢（每次都要重新编译全部依赖）。session 级只在整轮开始清一次，兼顾"干净起点"与"运行效率"。

### 4.4 pytest 标记与 CI 执行策略

#### 4.4.1 概念说明

随着测试用例变多，全量跑会非常慢。本项目用 pytest 的 **marker** 机制给用例分级：默认用例每次都跑；`low_priority` 用例只在定时任务/全量测试跑、PR 阶段跳过；`ci_skip` 用例在所有 CI 场景都跳过。CI 流水线按 GitHub 事件类型（PR / push / schedule）应用不同的 marker 过滤表达式，从而在 PR 阶段只跑核心用例、夜间再跑全量。这套机制让"快速反馈"与"充分覆盖"兼得。

#### 4.4.2 核心流程

```text
1. pyproject.toml 注册两个自定义 marker：low_priority、ci_skip
2. 用例用 @pytest.mark.low_priority / pytest.param(..., marks=...) 标注
3. CI 按事件选 marker 表达式：
     PR 事件        → -m "not (low_priority or ci_skip)"
     schedule/dispatch → -m "not ci_skip"
4. bench_test.sh --pytest-markers "<表达式>" 透传给 pytest
```

标签作用对照：

| 标签 | PR 事件 | 全量/定时任务 |
|------|---------|---------------|
| （无标签） | 执行 | 执行 |
| `low_priority` | **跳过** | 执行 |
| `ci_skip` | **跳过** | **跳过** |

#### 4.4.3 源码精读

**marker 注册**：

[pyproject.toml:24-28](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/pyproject.toml#L24-L28) —— 在 `[tool.pytest.ini_options]` 下注册 `low_priority` 与 `ci_skip`，注册后 `pytest --markers` 能看到，未注册的 marker 会被 pytest 警告。

**CI 事件 → marker 表达式映射**：

[.github/workflows/ci_cd.yml:532-539](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.github/workflows/ci_cd.yml#L532-L539) —— `schedule`/`workflow_dispatch`（定时/手动全量）用 `not ci_skip`（保留 low_priority）；`pull_request` 用 `not (low_priority or ci_skip)`（两者都跳过）。该 `PYTEST_MARKERS` 经环境变量透传进 bench_test.sh 的 `--pytest-markers`。

**marker 用法与 OR/AND 语义**：

[docs/pytest_marker_guide.md:21-26](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/pytest_marker_guide.md#L21-L26) —— 标签作用对照表；

[docs/pytest_marker_guide.md:31-37](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/pytest_marker_guide.md#L31-L37) —— CI 事件与 marker 过滤策略表。文档还强调一个易踩坑点：多个 `parametrize` 分别标参数时，标签按 **OR** 叠加（任一命中即标），无法表达 AND；要精确标某组合需把多参数合并成单个 `parametrize`、用 `pytest.param` 对元组打标。

**本地验证 marker**：

[docs/pytest_marker_guide.md:186-201](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/pytest_marker_guide.md#L186-L201) —— 给出一组本地复现 CI 行为的命令：`pytest -m "not (low_priority or ci_skip)"` 模拟 PR、`pytest --collect-only -m "low_priority"` 只收集不执行查看命中用例。

#### 4.4.4 代码实践

1. **实践目标**：学会用 marker 控制一个用例的执行时机，并本地模拟 CI 行为。
2. **操作步骤**：在一个测试函数上加 `@pytest.mark.low_priority`，然后跑 `pytest testing/python/ -m "low_priority" --collect-only -q` 与 `pytest testing/python/ -m "not (low_priority or ci_skip)" --collect-only -q`。
3. **观察现象**：第一条命令的收集结果里**包含**该用例；第二条**不包含**。
4. **预期结果**：这正好复现了"PR 阶段跳过 low_priority"的 CI 行为。
5. 选标签的直觉：耗时长的（如 pto + uint 组合）用 `low_priority`；有已知 bug/环境限制的用 `ci_skip`。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：你写了一个 `test_foo`，只想在每天夜间定时任务跑，该用什么标记？PR 阶段它的行为是？
**参考答案**：用 `@pytest.mark.low_priority`。PR 阶段（`pull_request` 事件）它会被 `-m "not (low_priority or ci_skip)"` 过滤掉而跳过；夜间 `schedule` 用 `not ci_skip`，会保留并执行。

**练习 2**：你想"仅当 dtype=uint16 且 target=pto 时"标 low_priority，直接用两个 parametrize 各标一个能达到目的吗？
**参考答案**：不能。两个 parametrize 上的标签按 OR 叠加，`dtype=uint16` 或 `target=pto` 任一命中都会被标。要实现 AND，需把 `(dtype, target)` 合并成单个 parametrize，对 `("uint16","pto")` 这个元组用 `pytest.param(..., marks=pytest.mark.low_priority)`。

### 4.5 贡献流程与代码格式化

#### 4.5.1 概念说明

`CONTRIBUTING.md` 是贡献流程总纲，规定「报 bug → 提问 → 提 PR → 仓库搭建 → 跑测试」的规范。核心要求两条：**每个 PR 必须带测试和文档**；**提交前必须跑 `./format.sh`**。`format.sh` 是一个集成了 yapf（格式化）、ruff（lint）、codespell（拼写）、clang-format（C/C++ 格式化）的四合一脚本，默认只格式化「相对 main 分支改动过的文件」，保证不改无关代码。

#### 4.5.2 核心流程

```text
贡献一个新算子的完整闭环：
1. 在 examples/<op>/ 下写 example_<op>.py，打印 Kernel Output Match!
2. （可选）在 testing/python/ 下补 test_tilelang_ascend_language_<op>.py 回归测试
3. （可选）在 docs/ 下补文档
4. ./format.sh            # 格式化改动的 .py / .cc / .h
5. bash examples/bench_test.sh --dirs <op>   # 本地验证 examples
6. python -m pytest testing/python/path/to/test_xxx.py -v   # 本地验证 pytest
7. git commit & 提 PR
```

#### 4.5.3 源码精读

**贡献总纲要求**：

[CONTRIBUTING.md:26-32](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CONTRIBUTING.md#L26-L32) —— 明确两条硬要求：提交前跑 `./format.sh`；每个 PR 必须包含 tests 和 docs。

[CONTRIBUTING.md:43-51](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CONTRIBUTING.md#L43-L51) —— 跑测试的官方方式：`python -m pytest testing`（与 bench_test.sh 内部调用的目录一致）。

**format.sh 的四件套**：

[CONTRIBUTING.md:30](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CONTRIBUTING.md#L30) —— CONTRIBUTING 要求「提交前跑 `./format.sh`」。实际 `format.sh` 会先做版本校验（yapf/ruff/codespell/clang-format 的版本必须匹配 `requirements-lint.txt`），再依次跑这四件套：yapf 格式化、codespell 拼写检查、ruff lint、clang-format 处理 C/C++。默认（无参数）只处理 `git diff` 相对 main 改动过的 `*.py/*.pyi` 与 `*.c/*.cc/*.cpp/*.h/*.hpp`；`--all` 处理全仓；`--files <f>` 处理指定文件。

**覆盖率采集（可选进阶）**：

[docs/coverage_guide.md:11-23](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/coverage_guide.md#L11-L23) —— `bash bench_test.sh --coverage --enable-cpp-coverage` 同时采 Python（`coverage run` + `pytest --cov`）与 C++（`lcov`）覆盖率，产物落在 `coverage_data/`。这呼应了 u6 各讲里"哪些 pass/codegen 被覆盖到"的验证需求。

#### 4.5.4 代码实践

1. **实践目标**：跑一遍 format.sh，看它对改动文件做了什么。
2. **操作步骤**：随便改一个 `examples/*.py`（比如多加一个空行），然后 `./format.sh --files examples/elementwise/elementwise_add.py`。
3. **观察现象**：yapf 会把不规范的格式改回项目风格；若文件已规范则无变化。
4. **预期结果**：脚本输出 `tile-lang yapf: Done` 等四段，最后若仍有未提交改动会提示 `Reformatted files. Please review and stage the changes.`
5. 提醒：format.sh 要求本机装了正确版本的 yapf/ruff/codespell（版本见 `requirements-lint.txt`），否则报版本错。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`format.sh` 不带参数时，会格式化全仓所有 `.py` 吗？
**参考答案**：不会。默认只格式化「相对 main 分支 merge-base 之后改动过」的文件（`format_changed`），避免污染无关代码。要全仓格式化需显式 `./format.sh --all`。

**练习 2**：CONTRIBUTING.md 对每个 PR 有哪两条硬性要求？
**参考答案**：①提交前跑 `./format.sh` 保证代码风格；②每个 PR 必须包含 tests 和 docs。

## 5. 综合实践

把本讲五个模块串起来，完成一次"迷你贡献"——新增一个 `my_op` 算子并让它被 bench_test.sh 自动发现并通过。

**任务**：在 `examples/my_op/example_my_op.py` 实现一个 Vector 核上的逐元素算子（如 `C = A * 2 + 1`，复用 u3-l5 学到的 `T.alloc_ub` / `T.copy` / `T.tile.*`），对拍后打印 `Kernel Output Match!`，确认 bench_test.sh 能发现并 `[PASSED]`。

**操作步骤**：

1. **建目录与脚本**：`mkdir -p examples/my_op`，新建 `example_my_op.py`，参考 `examples/elementwise/elementwise_add.py` 的骨架（带 `__main__` 守卫与 argparse 更规整）：

   ```python
   # 示例代码（非项目原有，请按本机 dtype/shape 调整）
   import argparse
   import tilelang
   from tilelang import language as T
   import torch

   tilelang.cache.clear_cache()

   @tilelang.jit(out_idx=[-1])
   def scale_shift(M, N, block_M, block_N, dtype="float"):
       m_num = M // block_M
       n_num = N // block_N
       VEC_NUM = 2

       @T.prim_func
       def main(A: T.Tensor((M, N), dtype), C: T.Tensor((M, N), dtype)):
           with T.Kernel(m_num * n_num, is_npu=True) as (cid, vid):
               bx = cid // n_num
               by = cid % n_num
               a_ub = T.alloc_ub((block_M // VEC_NUM, block_N), dtype)
               c_ub = T.alloc_ub((block_M // VEC_NUM, block_N), dtype)
               row = bx * block_M + vid * block_M // VEC_NUM
               with T.Scope("V"):
                   T.copy(A[row, by * block_N], a_ub)
                   T.tile.mul_scalar(c_ub, a_ub, 2.0)   # 注意：按本机 ascend_tile 实际 API 调整
                   T.copy(c_ub, C[row, by * block_N])
       return main

   if __name__ == "__main__":
       parser = argparse.ArgumentParser()
       parser.add_argument("--m", type=int, default=1024)
       parser.add_argument("--n", type=int, default=1024)
       args = parser.parse_args()
       M, N = args.m, args.n
       func = scale_shift(M, N, 128, 256)
       torch.manual_seed(0)
       a = torch.randn(M, N).npu()
       torch.npu.synchronize()
       c = func(a)
       ref = a * 2.0
       torch.testing.assert_close(c, ref, rtol=1e-2, atol=1e-2)
       print("Kernel Output Match!")
   ```

   > 注意：上面 `T.tile.mul_scalar` 仅为示意，请到 `tilelang/language/ascend_tile.py` 查实际可用的逐元素 API（如 `T.tile.mul` 配常量、或用 `T.Parallel` 写 `c_ub[...] = a_ub[...] * 2.0`）。本实践重点是"被 CI 认到"，算子内容可简化。

2. **本地跑通**：`cd examples/my_op && python example_my_op.py`，确认打印 `Kernel Output Match!`。
3. **验证自动发现**：`cd examples && bash bench_test.sh --dirs my_op --skip-pytest`，应看到 `Collected scripts from my_op: 1 files` 与 `[PASSED] ./my_op/example_my_op.py`。
4. **格式化**：`./format.sh --files examples/my_op/example_my_op.py`。
5. **（加分）补 pytest**：在 `testing/python/language/` 加 `test_tilelang_ascend_language_my_op.py`，用 4.3 的 pass_configs + parametrize 范式写两个 target 的回归，标 `low_priority` 的那个组合观察 4.4 的过滤行为。

**预期结果**：步骤 2 打印契约、步骤 3 `[PASSED]`、步骤 4 无残留格式问题。这就完成了一次符合项目规范的算子贡献闭环。若无 NPU，步骤 2/3 需在真机或 camodel 仿真（u7-l5）下完成，**待本地验证**。

## 6. 本讲小结

- `examples/` 下每个算子是一个"示例 + 自检 + 基线"三合一脚本，约定最后打印 `Kernel Output Match!`，这行字是 bench_test.sh 判定通过的契约。
- `bench_test.sh` 用 `collect_test_scripts` 按 `find -maxdepth 2 -name "*.py"`（排除 `__init__.py`/`*_golden.py`/`utils.py` 等）自动发现算子，并行执行后用大小写不敏感的正则匹配 `Kernel Output Match` / `Test Passed!` 判 PASS。
- 新算子要被自动认到，只需放进 `examples/<op>/`、扩展名 `.py`、文件名避开排除名单，深度 ≤ 2。
- `testing/python/` 的 pytest 用例用"工厂函数返回 prim_func + parametrize(target, shape)"范式，配 `clear_cache`/`setup_random_seed` 两个标准 fixture 与一组 Developer 模式 `pass_configs`。
- pytest 的 `low_priority`（PR 跳过、夜间跑）与 `ci_skip`（全跳过）两个 marker 在 `pyproject.toml` 注册，CI 按事件类型用 `-m "not (low_priority or ci_skip)"`（PR）或 `-m "not ci_skip"`（定时）过滤。
- 贡献闭环：写 examples（+ 可选 testing/docs）→ `./format.sh`（yapf/ruff/codespell/clang-format）→ 本地 bench_test.sh + pytest 验证 → 提 PR；CONTRIBUTING.md 要求每个 PR 带 tests 和 docs。

## 7. 下一步学习建议

- **回到 u7-l1/u7-l2**：本讲的"算子交付"是工程壳，真正的算子内容（FlashAttention 的 CV 协同、高性能 GEMM 的流水）在前面两讲；建议挑一个真实算子按本讲规范补一个 pytest 回归，把"会写"和"会交付"打通。
- **深入 CI**：阅读 `.github/workflows/ci_cd.yml` 的"改动检测 → 增量/全量分流 → marker 过滤"逻辑（4.4 引用的那段只是冰山一角），理解你的 PR 改了哪些文件会触发什么样的测试范围。
- **覆盖率驱动**：按 `docs/coverage_guide.md` 跑一次 `--coverage`，看看你新写的算子/测试覆盖了 `src/transform` 的哪些 pass，用覆盖率指导补测试（呼应 u6 各讲）。
- **上手 autotuner（u7-l6）**：交付算子后，下一步往往是调优；把本讲的 my_op 接上 `@tilelang.autotune`，对 block_M/block_N 做一次自动搜索，形成"写算子 → 交付测试 → 自动调优"的完整闭环。
