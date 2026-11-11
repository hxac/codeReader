# 测试体系与持续集成

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `tests/` 目录的组织规律：一个章节（或一个核心模块）对应一个测试文件，公共设施沉在 `conftest.py`。
2. 理解本项目最关键的测试设计——用 Dummy 替身替代真实模型，让生成循环、采样、投票这些「看似必须加载 0.6B 权重」的逻辑在无 GPU、无网络的 CI 上几秒内跑完。
3. 掌握 `SKIP_EXPENSIVE` 与 `RUN_REAL_DOWNLOAD_TESTS` 两个环境变量开关如何把测试分成「默认跑」「偶尔跑」「每周定时跑」三档。
4. 读懂 `.github/workflows/` 下的 CI 矩阵：多操作系统 × 多安装方式 × 多 PyTorch 版本，外加代码规范、链接与拼写检查。
5. 为 `top_p_filter` 或 `extract_final_candidate` 补写带边界情况的回归测试并跑通。

## 2. 前置知识

**单元测试与回归测试。** 单元测试是对一个函数的最小可重复检验：给定已知输入，断言输出等于预期值。回归测试则是「曾经错过的坑，从此用测试钉死」——以后任何人改动代码引入同样的错误，测试立刻变红。本仓库的测试大多属于这两类。

**pytest 的三条约定。** 本项目用 pytest 而非 unittest：

- 文件名以 `test_` 开头、测试函数以 `test_` 开头，pytest 会自动发现它们；
- 用原生 `assert` 断言，失败时 pytest 会打印出两边实际值；
- `conftest.py` 是 pytest 的「公共配置文件」，放在测试目录里会被自动加载，其中的 fixture（测试夹具）可以被所有测试文件使用，无需 import。

**测试替身（test double）。** 测一个生成循环，难道必须先加载一个 0.6B 的模型吗？不需要。我们可以写一个假模型类，它的 `__call__` 永远返回「把某个固定 token 的 logit 置为最大值」的张量——这样 `argmax` 每步都选中同一个 token，我们就能精确验证「EOS 提前停止」「生成恰好 N 个 token」这些行为。这种假对象叫测试替身。它把被测函数对外部的依赖（大模型）换成一个行为完全可控的东西，测试因此快、稳、可离线。

**环境变量当测试开关。** CI 和本地想跑的测试范围不同。本项目的做法不是维护两套测试文件，而是在测试文件顶部读环境变量（如 `os.environ.get("SKIP_EXPENSIVE", "0") == "1"`），再用 `@pytest.mark.skipif(...)` 决定跳不跳。这样同一份测试代码可以被不同环境「裁剪」。

**GitHub Actions 最小概念。** `.github/workflows/` 下每个 `.yml` 文件是一个 workflow（工作流），由事件触发（如 `pull_request`、`push`、定时 `schedule`）；workflow 里含 job（作业），job 跑在 GitHub 提供的虚拟机（runner）上；job 由 step（步骤）组成，每步是一条 shell 命令或一个现成 action（如 `actions/checkout@v4` 拉代码）。

**uv。** 本仓库 CI 统一用 [uv](https://docs.astral.sh/uv/) 管理依赖：`uv sync --group dev` 按 `pyproject.toml` 的 dev 依赖组搭建环境，`uv run pytest tests` 在该环境中执行测试。第 1 单元讲过 pip 与 uv 两条安装路线，本讲会看到 CI 把两条路线都测了一遍。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tests/conftest.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/conftest.py) | pytest 公共设施：`qwen3_weights_path` 会话级夹具（生成确定性测试权重）与 `import_definitions_from_notebook`（把 notebook 中的定义提取成 Python 模块） |
| [tests/test_ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch02.py) | 第 2 章测试：设备选择、贪心解码两代生成函数、吞吐统计，全部基于 Dummy 替身 |
| [tests/test_ch04.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py) | 第 4 章测试：温度缩放、top-p 过滤、温度采样生成、自一致性投票 |
| [tests/test_qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py) | 模型架构测试：RoPE/RMSNorm 与 Hugging Face 参考实现的等价性、下载入口的 mock 测试、环境开关的集中定义 |
| [tests/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/README.md) | 测试目录的自述文档：本地三种运行档位与 CI 对应关系 |
| [.github/workflows/tests-linux.yml](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/tests-linux.yml) | Linux 主测试 workflow，本讲精读对象 |
| [pyproject.toml](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml) | 声明 Python 版本要求、dev 依赖组（pytest/ruff 等）、ruff 规则与 pytest 选项 |

`tests/` 目录共 24 个 Python 文件，命名规律一目了然：`test_ch02.py` 到 `test_ch08.py` 对应第 2–8 章，`test_qwen3*.py` 对应模型架构（标准版、优化版、批量版、上下文极限），`test_appendix_*.py` 对应附录，另有针对独立脚本的 `test_ch07_scripts.py`、`test_math500_scripts.py` 与针对聊天裁剪的 `test_chat_trimming.py`（u10-l6 曾借用过它的技巧）。

## 4. 核心概念与源码讲解

### 4.1 测试目录组织与 conftest 公共设施

#### 4.1.1 概念说明

一个教学仓库最容易失控的地方是「教学代码」与「可维护代码」的边界模糊：notebook 里随手定义的函数改了名，配套包里的实现却忘了同步。本仓库的对策是把可复用实现沉到 `reasoning_from_scratch` 包（第 1 单元讲过），再为包里的每个模块配一个测试文件——测试文件名与章节一一对应，读者学完第 2 章的 `ch02.py`，就能去读 `test_ch02.py` 看这些函数被如何检验。

`conftest.py` 则是这套测试的「公共工具箱」。它目前提供两件工具：

1. **`qwen3_weights_path`**：一个 session 级 fixture。pytest 的 fixture 默认每个测试函数都重建一次，`scope="session"` 让它整个测试会话只建一次、所有测试共享——因为「造一份测试权重」是相对昂贵的操作。
2. **`import_definitions_from_notebook`**：把 Jupyter notebook 里的函数/类定义提取成一个真正的 Python 模块，让测试能直接测 notebook 代码，而不必执行整个 notebook。

#### 4.1.2 核心流程

`qwen3_weights_path` 的执行流程：

```text
pytest 启动
  └─ 首个测试请求 qwen3_weights_path 夹具
       ├─ 在临时目录建 models/ 子目录（tmp_path_factory，pytest 自动清理）
       ├─ download_qwen3_small(kind="base", tokenizer_only=True, ...)
       │    └─ 只下载分词器（真实小文件，u1-l3 讲过该入口）
       ├─ 若 qwen3_test_weights.pt 不存在：
       │    torch.manual_seed(123) → 新建 Qwen3Model(QWEN_CONFIG_06_B)
       │    → torch.save(model.state_dict(), ...) 保存确定性权重
       └─ 返回 base_path（目录路径）
```

`import_definitions_from_notebook` 的两遍扫描流程：

```text
nbformat 读取 notebook（as_version=4）
  Pass 1：只执行 import / from 语句
      └─ 逐行扫描，处理跨行括号配平（paren_balance）
         遇到 import 开头 → 收集到括号闭合 → exec 进模块命名空间
  Pass 2：只执行含 "def " 或 "class " 的 cell
      └─ 整个 cell 源码 exec 进同一命名空间
返回该模块对象
```

为什么分两遍？因为 notebook 的 cell 顺序不保证 import 都在最前面，而函数定义之间可能互相引用；先把所有 import 执行完，再执行所有定义，能最大化「定义可以被成功 exec」的概率——同时刻意跳过了顶层实验代码（绘图、下载、训练循环），只搬运「定义」。

#### 4.1.3 源码精读

会话级夹具与确定性权重（[tests/conftest.py:L17-L30](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/conftest.py#L17-L30)）：这段代码定义 `qwen3_weights_path`，先 `mktemp("models")` 建临时目录，调用 `download_qwen3_small(tokenizer_only=True)` 只取分词器，再在权重文件不存在时以固定种子 123 新建一个 `Qwen3Model` 并保存其 `state_dict`。注意它造的是**随机初始化**的权重——测试只关心「形状与流程走得通」，不关心这组权重能否生成有意义文本。

notebook 提取器的 Pass 1（[tests/conftest.py:L42-L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/conftest.py#L42-L66)）：这段逐行扫描 notebook cell，识别 `import ` / `from ` 开头的行并收集跨行 import，用 `paren_balance` 计数左右括号差值，归零即认为语句完整，随后 `exec` 进 `mod.__dict__`。`types.ModuleType` + `sys.modules[module_name]` 的组合让这个内存模块表现得像被正常 import 过一样。

Pass 2 只执行定义（[tests/conftest.py:L68-L74](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/conftest.py#L68-L74)）：遍历所有代码 cell，源码里含 `def ` 或 `class ` 字样的整段 `exec`。u10-l6 讲聊天界面时提到的「测试借 ast 只提取辅助函数避免加载权重」，正是同一思想在另一个测试文件里的变体。

这个工具的实际消费者是附录 C 测试：[tests/test_appendix_c.py:L17-L25](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_appendix_c.py#L17-L25) 从 conftest 导入它，把 `chC_main.ipynb` 的定义提取成模块再用；而 `qwen3_weights_path` 的消费者是 [tests/test_ch03.py:L366-L385](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L366-L385)，评测循环测试用它作为本地权重目录。

#### 4.1.4 代码实践

1. **实践目标**：熟悉 pytest 的收集行为，直观感受「一个章节一个测试文件」的映射。
2. **操作步骤**：在项目根目录执行（需先按第 1 单元装好环境）：

   ```bash
   uv run pytest tests --collect-only -q | head -40
   ```

   再对照输出数一数 `tests/` 下的文件名与 `ch02`–`ch08`、`chC`–`chG` 章节目录的对应关系，找出没有对应章节文件夹的测试（如 `test_qwen3_optimized.py` 对应 `ch02/03_optimized-LLM` 的优化实现）。
3. **需要观察的现象**：`--collect-only` 只列出测试不执行；输出里能看到每个测试的 `文件::函数` 二级地址（如 `tests/test_ch04.py::test_top_p_filter_truncates_and_renormalizes`）。
4. **预期结果**：收集到上百个测试项，无收集错误（若报 `ModuleNotFoundError: nbformat`，说明环境未装主依赖，回到第 1 单元重装）。具体数量随版本演进，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`qwen3_weights_path` 为什么用 `scope="session"` 而不是默认的 `scope="function"`？

**答案**：默认作用域下每个测试函数都会重新执行一遍夹具——重新下载分词器、重新实例化 0.6B 模型并保存，多个测试要用它时开销成倍放大。session 作用域让这份「造权重」的工作整个会话只做一次、结果共享，属于典型的「昂贵初始化上提」。

**练习 2**：`import_definitions_from_notebook` 为什么不直接 `exec` 整个 notebook？

**答案**：章节 notebook 里充满实验性顶层代码：下载权重、加载模型、跑生成、画图，完整执行既慢又需要网络与 GPU。测试只需要其中的函数与类定义，两遍扫描（先 import 后定义）恰好把「定义」搬进内存模块而跳过「执行」，让测试可以秒级检验 notebook 代码的结构正确性。

**练习 3**：夹具里为什么要写 `if not model_path.exists()` 这个判断？

**答案**：session 内夹具只执行一次，该判断主要防御「同一临时目录被重复使用」的情况（例如夹具将来被改成更大作用域或目录被复用），存在即跳过重建，属于幂等性保护；配合 `tmp_path_factory` 的每次会话新目录，正常路径下首次必为不存在。

### 4.2 测试替身：不加载权重也能测生成循环

#### 4.2.1 概念说明

第 2–4 章的生成函数（贪心解码、KV 缓存版、温度采样、top-p、自一致性投票）对外只有一个真正昂贵的依赖：模型本身。如果测试要加载真实 Qwen3 权重，那么每次跑测试都要下载 1.2 GB 文件、前向一个 28 层 Transformer——CI 上既慢又贵。

本仓库的解法是把模型换成 **Dummy 替身**：一个几十行的普通类，接口与真模型完全兼容（可调用、有 `eval()`、有 `reset_kv_cache()`、`cfg` 里带 `n_layers`），但 `__call__` 返回手工构造的 logits——把某个固定 token 列的值设为最大，于是 `argmax` 每步必选它。这样生成循环的**控制流**（何时停、生成几个、是否重置缓存、是否调用 eval）被完全锁定检验，而**数值计算**被绕过。这正是「测逻辑不测数值」的分层：数值正确性由 `test_qwen3.py` 里与 Hugging Face 参考实现的等价性测试负责（那类测试才需要真权重）。

#### 4.2.2 核心流程

以 `test_generate_text_basic_stops_on_eos` 为例：

```text
构造 DummyModel(fixed_token=2)          # 每步 argmax 恒为 token 2
输入 token_ids = [[1, 3, 4]]
调用 generate_text_basic(model, ids, max_new_tokens=10, eos_token_id=2)
    └─ 第一步：argmax → 2；2 == eos_token_id → 立即停止
断言：
  output.size(1) == 0                   # EOS 不进输出（u3-l1 讲过的检查位置）
  dummy_model.eval_called is True       # 副作用断言：生成函数确实调用了 eval()
```

替身还带**探针属性**（`eval_called`、`reset_called`）：被测函数对模型的副作用不再是黑盒，测试能断言「调用过」。这是替身模式比单纯 mock 更轻巧的地方——不需要 mock 库，手写布尔标志即可。

#### 4.2.3 源码精读

`DummyModel` 替身（[tests/test_ch02.py:L20-L36](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch02.py#L20-L36)）：`__call__` 按输入形状构造全零 logits，把 `fixed_token` 列置 1.0 使其成为 argmax 唯一赢家；`eval()` 记录被调用并返回自身，模仿 `nn.Module.eval()` 的链式返回。

带缓存接口的子类与分词器替身（[tests/test_ch02.py:L38-L50](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch02.py#L38-L50)）：`DummyModelCache` 补上缓存版生成函数需要的 `cfg["n_layers"]` 与 `reset_kv_cache()`（探针 `reset_called`）；`DummyTokenizer.decode` 把 token 列表拼成字符串——注意它实现的是「逐 token」解码语义而非真 BPE。

EOS 提前停止测试（[tests/test_ch02.py:L87-L99](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch02.py#L87-L99)）：把 `eos_token_id` 设成 `fixed_token`，第一步生成即命中 EOS，断言输出长度为 0 且 `eval_called` 为真。配套的无 EOS 测试（[tests/test_ch02.py:L102-L112](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch02.py#L102-L112)）则断言恰好生成 `max_new_tokens` 个且全部等于 `fixed_token`——一个替身锁两种行为。

top-p 过滤的两个测试（[tests/test_ch04.py:L82-L107](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L82-L107)）：这里连模型替身都不需要——`top_p_filter` 是纯张量函数，直接喂概率向量 `[0.5, 0.4, 0.1]`、`top_p=0.6`，断言过滤重归一化后等于 `[0.5556, 0.4444, 0]`（核保留前两个 token，u5-l2 手算过同一例子）；批量行测试则断言每行独立归一化、和为 1。这是测试分层的另一个极端：**输入输出都是普通张量的函数，测试就是一个数学等式**。

用 `monkeypatch` 测自一致性投票（[tests/test_ch04.py:L129-L163](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch04.py#L129-L163)）：`monkeypatch.setattr(ch04, "generate_text_stream_concat_flex", wrapped_generate)` 把模块内的生成函数替换成一个按调用次数轮换返回 `"2", "2", "3"` 的假货，于是 `self_consistency_vote` 在完全不碰模型的情况下被喂了三次已知答案，断言多数票结果 `final_answer == "2"`、`counts["2"] == 2`。monkeypatch 与 Dummy 的分工：Dummy 替换「传进函数的对象」，monkeypatch 替换「函数在模块里引用的名字」。

#### 4.2.4 代码实践

1. **实践目标**：亲自运行本讲主角测试，观察替身测试的速度与输出。
2. **操作步骤**：在项目根目录执行：

   ```bash
   SKIP_EXPENSIVE=1 RUN_REAL_DOWNLOAD_TESTS=0 uv run pytest tests/test_ch02.py tests/test_ch04.py -x -v
   ```

   `-x` 表示首败即停，`-v` 显示每个测试名。若用 pip 路线安装，把 `uv run` 去掉、确认在激活的虚拟环境里执行即可。
3. **需要观察的现象**：测试几乎瞬间完成（无权重加载、无网络）；`-v` 列出两文件的全部测试项，包括可能显示 `SKIPPED` 的项。
4. **预期结果**：两个文件合计收集 13 个测试（`test_ch02.py` 7 个、`test_ch04.py` 6 个），其中 `test_get_device_tf32_setting_is_compatible_with_torch_compile` 在 PyTorch 版本不在 2.9–2.10 区间时会因 `pytest.skip` 显示跳过（见 [tests/test_ch02.py:L59-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch02.py#L59-L62)）；其余全部通过。具体跳过与否取决于本地 torch 版本，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`DummyModel.__call__` 为什么把 `out[..., self.fixed_token] = 1.0` 而不是随机值？

**答案**：生成函数用 `argmax` 选 token，全零背景上置 1.0 保证 `fixed_token` 列严格最大、argmax 结果确定，测试因此可以精确断言「生成的每个 token 都等于 fixed_token」。随机值会让 argmax 不可预测，断言无从写起。

**练习 2**：`test_self_consistency_vote_majority` 里为什么要在 fake 函数外再包一层 `wrapped_generate` 计数？

**答案**：假生成函数需要按调用次序轮换返回 `"2", "2", "3"`，而它被调用时无法自知是第几次；外层包装器用闭包里的 `call_counter["i"]` 给每次调用注入 `_call_idx`，把「无状态的假函数」变成「按序吐答案的假函数」，模拟三次温度采样的多样结果。

**练习 3**：如果现在改坏 `top_p_filter`（比如把 `keep[:, 0] = True` 兜底删掉），`test_top_p_filter_batched_rows_renormalize_independently` 一定能抓到吗？

**答案**：不一定。该批量测试用 `top_p=0.70`，两行的首个 token 前缀质量都是 0，本来就会被保留，删掉兜底不影响这个用例；能稳定抓住该回归的是极小 `top_p` 的边界用例——这正是本讲综合实践要补写的测试（见第 5 节），也说明「边界情况必须显式覆盖」。

### 4.3 双环境开关：SKIP_EXPENSIVE 与 RUN_REAL_DOWNLOAD_TESTS

#### 4.3.1 概念说明

同一份测试，不同场景想要的范围不同：改一行注释后想秒级验证；周末想跑完整套；而「真实下载大权重」的集成测试既贵又依赖外部服务器存活，只适合偶尔抽查。本仓库用两个环境变量把测试切成三档：

| 档位 | 命令 | 含义 |
| --- | --- | --- |
| 默认（CI 同款） | `SKIP_EXPENSIVE=1 RUN_REAL_DOWNLOAD_TESTS=0` | 跳过昂贵测试与真实下载，最接近 GitHub CI 矩阵 |
| 全量离线 | `SKIP_EXPENSIVE=0 RUN_REAL_DOWNLOAD_TESTS=0` | 加回昂贵但离线的测试 |
| 真实下载 | `SKIP_EXPENSIVE=0 RUN_REAL_DOWNLOAD_TESTS=1 ... -k real_download` | 只跑名字含 `real_download` 的网络集成测试 |

配套还有**确定性设置**：CI 虚拟机线程数、内核调度都与本地不同，浮点归约顺序一变结果就漂。`test_qwen3.py` 顶部干脆把线程钉死为 1、开 `torch.use_deterministic_algorithms(True)`，让「同一个 commit 在任何机器上断言同一组数字」成为可能——这正是 [tests/test_qwen3.py:L345-L379](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L345-L379) 里 `check_model_generation` 敢断言生成 token 序列精确等于 `[15, 11, 1, 1, 1]` 的前提（它还刻意用公式填充参数而非随机初始化，注释写明「seeded values can change across PyTorch versions」）。

#### 4.3.2 核心流程

开关的生效链路：

```text
CI workflow 设置 env: SKIP_EXPENSIVE: "1"
（或本地命令行前缀 SKIP_EXPENSIVE=1）
   ↓
测试文件模块顶层读取：
   skip_expensive = os.environ.get("SKIP_EXPENSIVE", "0") == "1"
   run_real_download = os.environ.get("RUN_REAL_DOWNLOAD_TESTS", "0") == "1"
   ↓
@pytest.mark.skipif(条件, reason="...")
   ↓
pytest 收集阶段逐个求值 → 命中条件的测试标记 SKIPPED，不执行
```

`-k real_download` 则是 pytest 的另一机制：按测试函数名子串过滤，与 skipif 正交——README 推荐的真实下载命令同时用了开关（放行）与 `-k`（只选这类）。

#### 4.3.3 源码精读

开关变量的定义（[tests/test_qwen3.py:L81-L83](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L81-L83)）：模块顶层把两个环境变量读成布尔值，默认 `"0"`（不跳过、不跑真实下载）。各测试文件按需复制这两个变量——[tests/test_qwen3.py:L159-L163](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L159-L163) 的分词器等价性测试要求**同时**满足「非 skip_expensive、开 real_download、装了 transformers」三个条件才执行，reason 里写明了开启方式，失败信息可自查。

确定性环境（[tests/test_qwen3.py:L34-L39](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L34-L39)）：把 MKL/OMP 线程数钉为 1、禁 mkldnn、单线程 torch、开确定性算法。注释直言目的是「Make CI more reproducible & robust」。

本地运行档位与 CI 对应关系（[tests/README.md:L13-L28](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/README.md#L13-L28) 与 [tests/README.md:L82-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/README.md#L82-L98)）：README 明确说默认档「closest local equivalent to the default GitHub test matrix」，并列出真实下载测试覆盖的六个文件——`test_ch03.py`（MATH-500 与分词器下载）、`test_ch06.py`（训练集下载）、`test_ch07.py`（GitHub raw 文件）、`test_ch08.py`（蒸馏数据集）、`test_appendix_d.py`（1.7B 快照）、`test_qwen3.py`（0.6B 分词器对比）。

依赖与工具声明（[pyproject.toml:L46-L56](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L46-L56)）：pytest、ruff、pytest-ruff 等都在 dev 依赖组而非运行依赖——`uv sync --group dev` 一次性装齐测试工具链；`huggingface-hub` 与 `safetensors` 也在此组，正对应附录 D 真实下载测试的需要。

#### 4.3.4 代码实践

1. **实践目标**：体验开关对测试范围的裁剪效果。
2. **操作步骤**：

   ```bash
   # 默认档：CI 同款
   SKIP_EXPENSIVE=1 RUN_REAL_DOWNLOAD_TESTS=0 uv run pytest tests/test_qwen3.py -v

   # 对比：放开昂贵测试（仍然离线）
   SKIP_EXPENSIVE=0 RUN_REAL_DOWNLOAD_TESTS=0 uv run pytest tests/test_qwen3.py -v
   ```

3. **需要观察的现象**：两次运行的收集总数相同，但第一次中带 `skipif(skip_expensive...)` 条件的测试显示 `SKIPPED`，第二次它们真正执行（其中依赖真实下载的仍被 `run_real_download` 条件挡住）。
4. **预期结果**：例如 `test_qwen3_base_equivalence_with_transformers` 这类与 HF 参考实现对照的测试，其执行与否随开关变化；具体哪些项跳过以 `-v` 输出为准，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么开关用环境变量而不是 pytest 的 `-k` 或 `--deselect`？

**答案**：`-k` 按名字子串筛，昂贵测试散布在多个文件、命名并不统一，逐个列名不可维护；环境变量让「跳过策略」写在测试自身的 `skipif` 条件里（就近声明依赖成本），CI 与本地只需改一个环境变量就能整体切换档位，且条件永远跟随代码演进。

**练习 2**：`check_model_generation` 为什么用 `values = torch.arange(...)` 的公式填参数而不是 `torch.manual_seed` 随机初始化？

**答案**：随机初始化的数值依赖框架的随机数实现，跨 PyTorch 版本可能改变，黄金输出 `[15, 11, 1, 1, 1]` 就会漂移失效；确定性的公式填充与框架随机机制无关，只要算子语义不变结果就不变，测试寿命更长。

**练习 3**：真实下载测试为什么单独放在每周定时的 workflow，而不是跟着每个 PR 跑？

**答案**：它们要下载大 checkpoint、依赖外部服务器可用，放进 PR 矩阵会拖慢每次提交且容易被第三方故障误伤为「代码错误」；每周定时抽查足以发现「链接失效、服务器下线」这类问题，职责分离让快反馈通道（PR 测试）保持纯净。

### 4.4 CI 矩阵与质量守门

#### 4.4.1 概念说明

`.github/workflows/` 下有 13 个 workflow，可以分成四类：

1. **平台矩阵**：`tests-linux.yml`、`tests-macos.yml`、`tests-windows.yml` 在三个操作系统上跑同一套默认档测试——`reasoning_from_scratch` 声称 `OS Independent`（[pyproject.toml:L43](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L43)），CI 用真机兑现承诺。
2. **安装器与版本矩阵**：`basic-tests-pip.yml` 用纯 pip + venv 路线验证「不装 uv 的用户」能跑；`basic-tests-old-pytorch.yml` 钉住 `torch==2.10.0`（书中原始版本）防「只在新版 torch 上好」；`basic-test-nightly-pytorch.yml` 装 nightly 版面向未来（其测试步骤当前被注释禁用，属占位）。
3. **质量守门**：`code-linter.yml` 跑 ruff 代码规范、双引号风格检查与 notebook 行长检查；`check-spelling-errors.yml` 跑 codespell；`check-links.yml` 用 pytest-check-links 扫全仓库超链接。
4. **辅助调度**：`real-download-tests.yml` 每周定时跑真实下载测试；另有 `required-checks.yml`、`retry-required-checks.yml`、`notify-ci-success.yml` 等聚合与通知类 workflow。

#### 4.4.2 核心流程

以 `tests-linux.yml` 为例，一次 PR 的完整链路：

```text
开发者向 main 提交 pull_request
  ↓ paths 过滤：只有 .py/.ipynb/.yaml/.yml/.sh 变化才触发
  ↓（并发去重：同一 PR 新推送会取消旧任务，省配额）
job uv-tests 在 ubuntu-latest 上：
  1. actions/checkout@v4 拉代码
  2. actions/setup-python@v5 装 Python 3.13
  3. curl 安装 uv → uv sync --group dev（含 pytest/ruff）
  4. 先跑 tests/test_ch07_scripts.py（第 7 章独立脚本冒烟）
  5. uv run pytest tests（默认档，SKIP_EXPENSIVE=1 已在 env 设置）
  ↓ 任一步非零退出码 → 该 PR 的检查标红
```

#### 4.4.3 源码精读

触发与节流（[.github/workflows/tests-linux.yml:L7-L28](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/tests-linux.yml#L7-L28)）：`on` 同时声明 `push`（仅 main 分支）与 `pull_request`（目标 main），两者都带 `paths` 过滤——改 Markdown 文档不会触发代码测试；`workflow_dispatch` 允许手动触发。`concurrency` 组以 workflow 名 + 分支引用为键、`cancel-in-progress: true`，使同一 PR 连续推送时旧任务自动取消。

测试步骤（[.github/workflows/tests-linux.yml:L30-L53](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/tests-linux.yml#L30-L53)）：job 级 `env` 设 `SKIP_EXPENSIVE: "1"`（第 4.3 节开关在 CI 侧的源头）；Python 版本钉 `"3.13"`；安装 uv 后 `uv sync --group dev`；值得注意的是**先单独跑 `test_ch07_scripts.py`** 再跑全量——第 7 章训练脚本最重，把它前置可以在它挂掉时快速定位，而不必等全量跑完才看到失败。

pip 路线的差异（[.github/workflows/basic-tests-pip.yml:L43-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/basic-tests-pip.yml#L43-L54)）：建 venv、`pip install -r requirements.txt`、`pip install .`、只补装 pytest，然后 `pytest tests --ignore=tests/test_ch08_huggingface.py`——被忽略的文件需要 dev 组里的 `transformers`，pip 路线没装，这个 `--ignore` 就是两条安装路线依赖差异的显式记录。

旧版 torch 钉扎（[.github/workflows/basic-tests-old-pytorch.yml:L46-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/basic-tests-old-pytorch.yml#L46-L54)）：`uv add pytest-ruff torch==2.10.0` 把 torch 降回书中原始版本再跑全量测试，守护「最低支持版本」不腐烂——与 [pyproject.toml:L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L27) 声明的 `torch>=2.10,<3` 下界互为印证。

每周真实下载调度（[.github/workflows/real-download-tests.yml:L7-L10](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/real-download-tests.yml#L7-L10) 与 [L20-L46](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/real-download-tests.yml#L20-L46)）：只由 `workflow_dispatch`（手动）与 `cron: '17 8 * * 1'`（每周一 08:17 UTC）触发，不在 PR 矩阵里；`timeout-minutes: 180` 给大下载留足余量；`RUN_REAL_DOWNLOAD_TESTS: "1"` 配 `-k real_download` 只挑六个文件里的真实下载测试。

代码规范三连（[.github/workflows/code-linter.yml:L42-L59](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/code-linter.yml#L42-L59)）：`ruff check .`（规则集见 [pyproject.toml:L64-L65](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L64-L65)，选 E4/E7/E9/F 即错误级与 pyflakes，不做风格吹毛求疵）+ 仓库自写的双引号检查脚本 + 四个主章节 notebook 的行长检查。拼写检查（[.github/workflows/check-spelling-errors.yml:L29-L32](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/check-spelling-errors.yml#L29-L32)）用 codespell 扫 `txt/md/py/ipynb`，`-L` 白名单忽略若干常见变体。链接检查（[.github/workflows/check-links.yml:L45-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/check-links.yml#L45-L54)）借 pytest-check-links 插件全仓扫 URL，带超时与重试参数、忽略一批反爬严格的站点。

#### 4.4.4 代码实践

1. **实践目标**：把规格要求的三项观察落实成文字。
2. **操作步骤**：打开 [`.github/workflows/tests-linux.yml`](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/.github/workflows/tests-linux.yml)，逐行回答：① 它安装的 Python 版本；② 最终执行的 pytest 命令；③ 触发它的 `pull_request` 事件有何附加条件。
3. **需要观察的现象**：三处信息分别位于 setup 步骤、最后一个 run 步骤、`on:` 块。
4. **预期结果**：① Python `3.13`（L42）；② 两条命令——`uv run pytest tests/test_ch07_scripts.py` 与 `uv run pytest tests`（L50、L53）；③ `pull_request` 限定目标分支 `main`，且仅当改动匹配 `**/*.py`、`**/*.ipynb`、`**/*.yaml`、`**/*.yml`、`**/*.sh` 之一时触发（L16-L23）。

#### 4.4.5 小练习与答案

**练习 1**：`code-linter.yml` 的 job 名叫 `flake8`，实际跑的是什么？这说明什么？

**答案**：实际跑 ruff（步骤名自注「a faster flake8 equivalent」）加两个自写检查脚本。job 名是工具迁移留下的历史痕迹—— lint 工具从 flake8 换成 ruff 时没改标识符；读 CI 时应以步骤命令为准，名字可能过时。

**练习 2**：为什么 `tests-linux.yml` 要把 `test_ch07_scripts.py` 单独放在全量测试之前跑一步？

**答案**：这是「快速失败」排序——第 7 章独立训练脚本最重、最容易出问题，前置冒烟能让相关失败在日志最上方独立呈现、更快定位；即便它挂了，全量步骤也会随之不再执行，节省 CI 时间。

**练习 3**：如果有人只改了 `README.md` 里的一个错字并提交 PR，`tests-linux.yml` 会跑吗？`check-spelling-errors.yml` 呢？

**答案**：`tests-linux.yml` 不会跑——它的 `paths` 过滤只认 `.py/.ipynb/.yaml/.yml/.sh`，Markdown 不在列。而 `check-spelling-errors.yml` 的触发没有 `paths` 过滤（只有分支条件），任何 PR 都会跑拼写检查，改错字恰好被它把关。

## 5. 综合实践

**任务：为 `top_p_filter` 补写一组带边界情况的回归测试，并全链路验证。**

背景：4.2.5 的练习 3 已经揭示——现有测试对「`top_p` 极小时兜底只留一个 token」这条路径（[reasoning_from_scratch/ch04.py:L208-L209](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L208-L209) 的 `keep[:, 0] = True`）没有显式覆盖。现在补上它。

**步骤一：新建测试文件** `tests/test_u10_practice.py`（新文件，不动任何现有代码）：

```python
# 示例代码：为 top_p_filter 补写的边界回归测试
import torch

import reasoning_from_scratch.ch04 as ch04


def test_top_p_filter_degenerate_single_token_distribution():
    # 边界 1：分布已退化为单点分布，过滤后应保持不变
    probas = torch.tensor([[1.0, 0.0, 0.0]])
    filtered = ch04.top_p_filter(probas, top_p=0.5)
    assert torch.allclose(filtered, probas)


def test_top_p_filter_tiny_top_p_falls_back_to_single_token():
    # 边界 2：top_p 极小，兜底逻辑应只保留最大概率 token 并归一化
    probas = torch.tensor([[0.3, 0.3, 0.4]])
    filtered = ch04.top_p_filter(probas, top_p=0.0001)
    assert torch.allclose(filtered, torch.tensor([[0.0, 0.0, 1.0]]))


def test_top_p_filter_none_is_identity():
    # 边界 3：top_p=None 走短路分支，直接返回原张量对象
    probas = torch.tensor([[0.2, 0.5, 0.3]])
    out = ch04.top_p_filter(probas, top_p=None)
    assert out is probas
```

**步骤二：运行并确认通过**：

```bash
SKIP_EXPENSIVE=1 RUN_REAL_DOWNLOAD_TESTS=0 uv run pytest tests/test_u10_practice.py -v
```

预期 3 个测试全部通过。断言依据可自行核对 [reasoning_from_scratch/ch04.py:L194-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L194-L222)：边界 2 中排序后前缀质量 `[0, 0.4, 0.7]` 全都不小于 0.0001，只有兜底行保住第一个 token，scatter 回原位再归一化即 `[0, 0, 1]`。若想改成测 `extract_final_candidate`（u4-l2 的三级回退），可仿照同样的「嵌套 boxed / 纯文本数字 / 无数字空文本」三边界结构替换。

**步骤三：验证它会进 CI**：执行 `uv run pytest tests --collect-only -q | grep u10_practice`，确认新文件被收集；再对照第 4.4 节——因为它匹配 `**/*.py` 的 paths 过滤，这个 PR 一旦推送，`tests-linux.yml` 的 `uv run pytest tests` 会自动把它跑进矩阵。

**步骤四（回归验证）**：临时注释掉 `ch04.py` 里的 `keep[:, 0] = True` 一行再跑测试，观察边界 2 是否变红；验证完务必还原。（此步会临时改动源码，仅限本地实验，不要提交。）

以上命令的运行结果请以本地实际输出为准（待本地验证）。

## 6. 本讲小结

- `tests/` 按「一章一文件、一模块一文件」组织，公共设施沉在 `conftest.py`：session 级 `qwen3_weights_path` 夹具一次造确定性权重全程共享，`import_definitions_from_notebook` 两遍扫描（先 import 后定义）让测试可以直接检验 notebook 里的定义而不执行 notebook。
- 测试替身是本仓库测试体系的灵魂：`DummyModel`/`DummyModelCache` 用固定 logits 锁死 argmax 结果与副作用探针（`eval_called`、`reset_called`），让生成循环、EOS 停止、缓存重置这些逻辑在无 GPU 无网络的 CI 上秒级验证；`monkeypatch` 则替换模块内名字，喂已知答案测投票。
- 纯张量函数（如 `top_p_filter`）的测试就是一个数学等式：给定概率向量与 `top_p`，断言过滤重归一化后的精确值。
- `SKIP_EXPENSIVE` 与 `RUN_REAL_DOWNLOAD_TESTS` 两个环境变量配 `skipif` 把测试裁成「CI 默认档 / 全量离线档 / 每周真实下载档」，`test_qwen3.py` 顶部的确定性设置（单线程 + 确定性算法 + 公式化参数填充）保证跨机器跨版本的断言稳定。
- CI 是一个 13-workflow 的矩阵：三平台测试 × pip/uv 两条安装路线 × 当前/旧版 torch，外加 ruff+引号+notebook 行长的代码规范、codespell 拼写、pytest-check-links 链接检查；`real-download-tests.yml` 每周一 08:17 UTC 定时抽查六个文件的真实下载。
- `paths` 过滤与 `concurrency` 取消让 CI 只在代码相关变更时触发并自动去重，`tests-linux.yml` 还把最重的第 7 章脚本冒烟前置以快速失败。

## 7. 下一步学习建议

本讲是学习手册的最后一讲。至此你已经走完了全书主线：Qwen3 架构（单元二）→ 生成与采样（单元三、五）→ 评测与验证器（单元四）→ 推理时扩展（单元五、六）→ RLVR/GRPO（单元七、八）→ 蒸馏与生态互通（单元九）→ 工程与专题（单元十）。接下来建议：

1. **以贡献者身份回到仓库**：挑一个你曾有疑问的函数（比如 `top_p_filter` 或 `extract_final_candidate`），按本讲综合实践的格式给它补边界测试，体会「测试是理解代码的终极方式」。
2. **通读一遍没细讲的测试文件**：`tests/test_qwen3_optimized.py`（对照 u10-l1 的 SDPA 实现）、`tests/test_qwen3_batched_stop.py`（对照 u10-l2 的双坐标系停止逻辑）、`tests/test_ch07_scripts.py`（CI 最先跑的那个文件），看它们如何分别为「数值等价」「逐行停止」「脚本冒烟」设计断言。
3. **横向对比其他开源项目的测试体系**：如 vLLM、axolotl 的 CI 矩阵与 nightly 测试设计，思考「教学仓库」与「生产仓库」在测试取舍上的差异——前者重可读与可复现，后者重性能回归与硬件覆盖。
4. **延伸阅读**：pytest 官方文档的 fixtures 与 monkeypatch 章节、GitHub Actions 的路径过滤与并发取消文档、ruff 规则分类（理解本项目为何只选 E4/E7/E9/F）。
