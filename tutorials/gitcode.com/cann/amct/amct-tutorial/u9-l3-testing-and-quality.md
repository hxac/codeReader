# 测试体系、代码规范与 CI

## 1. 本讲目标

AMCT 是一个体量大、模块多的量化工具包（既有 Python 算法流程，又有 C++/Ascend C 的 NPU 算子）。这种项目要长期维护、多人协作，靠的就是三道防线：**单元测试**守住行为不变、**代码规范**守住风格一致、**CI 流水线**把前两道防线自动化到每一次提交。

学完本讲，你应当能够：

1. 读懂 `pyproject.toml` 中的 pytest 配置，理解 `cpu`/`npu`/`slow` 三个标记的实际用途与「声明 vs 使用」的差距。
2. 看懂 `tests/unit_test/` 目录如何按 `algorithms`/`quantization`/`workflows`/`common`/`classic`/`cli` 镜像源码结构，并理解 `conftest.py` 的 `autouse` fixture 如何在不改测试代码的前提下统一种子与清理。
3. 说出 pre-commit 如何用 ruff（Python）+ clang-format（C++）在提交前拦截不规范代码。
4. 把一篇算法/模块的单元测试文件（如 `test_awq.py`）拆开，说明它用哪些手段隔离被测函数、覆盖了哪些边界。
5. 描述 `.gitcode/workflows/` 下 GitCode CI 流水线的四个 stage，知道单测与覆盖率在哪个阶段跑。

本讲是全手册的收口篇之一：前面各讲分别拆解了算法（u6）、BitPolicy（u3-l4）、量化模块（u7）、deploy（u4-l4），本讲则回答「这些模块如何被测试守护、如何被 CI 自动校验」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，什么是单元测试与 pytest。** 单元测试是「对一个最小代码单元（通常是一个函数）单独验证其输入输出是否符合预期」的实践。Python 生态里 `pytest` 是事实标准：它扫描以 `test_` 开头的函数去执行，用 `assert` 断言，断言失败即该测试失败。`pytest.mark.parametrize` 可以把一组参数喂给同一个测试函数，省去重复写多个测试。

**第二，什么是覆盖率（coverage）。** 覆盖率回答「测试到底跑过了产品代码的哪些行」。`coverage` 工具在测试运行时记录被解释器执行到的行，最后给出「已覆盖行数 / 总行数」的比例。它是「测试有没有漏」的参考指标，但不是唯一指标——100% 覆盖率不代表覆盖了所有分支。

**第三，什么是 pre-commit 与 CI。** `pre-commit` 是一个挂在 `git commit` 之前的钩子框架：提交时代码还没真正入库，先跑一遍格式化/静态检查，挂了就拒绝提交，把问题挡在源头。CI（Continuous Integration，持续集成）则在代码推到远端、发起合并请求（PR/MR）时，由云端机器跑一整套编译、测试、检查。AMCT 的 CI 不在常见的 `.github/workflows`（GitHub Actions）里，而在 `.gitcode/workflows/`（GitCode 平台流水线），结构相似但语法不同。

需要澄清一个术语：本讲的 **标记（marker）** 指 pytest 的 `@pytest.mark.xxx`，是给测试贴的分类标签；它与量化里反复出现的 `--quant_target`（mlp/moe/attn-linear/attn-cache，见 u1-l4）是完全不同的概念，只是恰好都叫「target/标记」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pyproject.toml` | 项目总配置：定义 pytest 的 `testpaths`/`markers`/`addopts`、coverage 的 `source`/`omit`/`exclude_lines`、ruff 格式化风格 |
| `tests/unit_test/conftest.py` | unit_test 根级共享 fixture：临时目录清理、随机种子固定、`cpu_device` 设备 fixture |
| `tests/unit_test/algorithms/test_awq.py` | AWQ 算法单元测试样本：展示如何用 monkeypatch 隔离 `quant_dequant_tensor` 并覆盖各种边界 |
| `tests/unit_test/quantization/test_bit_policy.py` | BitPolicy 单元测试样本（承接 u3-l4）：覆盖 `linear_bits` 回退、校验、yaml 解析 |
| `tests/unit_test/workflows/test_llm_deploy.py` | LlmDeployWorkflow 单元测试样本（承接 u4-l4）：大量 `parametrize` 与 mock 拆解 deploy 的文件 IO 逻辑 |
| `.pre-commit-config.yaml` | pre-commit 钩子配置：clang-format（C++）+ ruff-check/ruff-format（Python） |
| `.gitcode/workflows/amct_action.yml` | GitCode CI 主编排流水线：PreBuild → compile → ut → PreSmoke 四 stage |
| `.gitcode/workflows/ut_action.yml` | UT 子流水线：跑 `ut.sh` 后用 `ut-cov-report` 上报覆盖率 |
| `.gitcode/workflows/codecheck_action.yml` | 代码检查子流水线：含 precommit（即 pre-commit）、check-pr、SCA 安全扫描 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：pytest 配置与标记体系、unit_test 测试组织与 conftest 机制、pre-commit 代码规范、GitCode CI 流水线。

### 4.1 pytest 配置、三标记体系与 coverage

#### 4.1.1 概念说明

pytest 启动时会读取项目里的 `[tool.pytest.ini_options]` 段，它决定了「测试从哪里发现」「默认带哪些参数」「测试可以贴哪些标签」。AMCT 把这些配置全部集中在 `pyproject.toml`，而不是单独的 `pytest.ini` 或 `setup.cfg`，这是 PEP 518 之后 Python 项目「一个配置文件管所有工具」的惯例。

与 pytest 配套的是覆盖率配置 `[tool.coverage.*]`，它告诉 coverage 工具「统计哪些源码、忽略哪些目录、哪些行不算未覆盖」。两者一起放在 `pyproject.toml` 里，是因为 CI 跑测试时往往是 `pytest ... --cov` 一条命令，配置共享同一处最方便。

#### 4.1.2 核心流程

pytest 一次运行的决策链路如下（伪代码）：

```
1. 读 pyproject.toml [tool.pytest.ini_options]
2. testpaths 决定去哪几个目录找测试        → AMCT 有两个根
3. addopts 把默认参数追加到命令行            → "-ra" 永远生效
4. env 在进程级注入环境变量                 → 关掉 torch 后端自动加载
5. 收集所有 test_ 函数，按 -m 表达式做标记筛选
6. markers 白名单里声明过的标记才合法        → 未声明的标记会告警
7. 执行；若带 --cov，coverage 按 [tool.coverage.run] 统计 source 行
8. 测试结束，coverage 按 [tool.coverage.report] 输出未覆盖行
```

关键点：`markers` 是**白名单声明**——你声明了 `cpu`/`npu`/`slow`，pytest 才认得它们、且不会对用这些标记的测试报「unknown mark」警告。但声明只是「允许用」，**不等于已经被用上**（这个差距在 4.1.3 会用真实数据说明）。

#### 4.1.3 源码精读

先看 pytest 配置段：

[pyproject.toml:L21-L31](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/pyproject.toml#L21-L31) — 这是 pytest 的全部入口配置：`testpaths` 指向两个测试根（`tests/unit_test` 新式 LLM PTQ 测试 + `tests/amct_pytorch` 经典测试），`markers` 声明三个标签，`addopts = "-ra"` 让默认输出展示「除通过外所有测试的摘要」，`env` 注入 `TORCH_DEVICE_BACKEND_AUTOLOAD=0` 防止 torch 自动加载后端（避免在没有 NPU 的 CI 上误触发 torch_npu）。

三个标记的注释写得很直白，对应三种运行成本：

- `cpu: tests that run on CPU only and are fast (default)` —— 纯 CPU、快，默认档。
- `npu: tests that require an Ascend NPU` —— 必须有昇腾 NPU 才能跑。
- `slow: tests that take more than a few seconds` —— 慢测试。

但**声明不等于使用**。在本仓库里实际统计：

- `@pytest.mark.cpu` 在 `tests/` 下被应用约 18 处（典型在 `test_deploy_export.py`、`test_model_adapters_mocked_glm5.py`）。
- `@pytest.mark.npu` 与 `@pytest.mark.slow` 一次都没被应用——它们目前主要是**为 `-m` 选择预留的标签**。

真正需要 NPU 的测试并不是贴 `@pytest.mark.npu`，而是用 `skipif` 守卫，例如：

[tests/unit_test/quantization/test_dtypes.py:L387-L390](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/quantization/test_dtypes.py#L387-L390) — 用 `@pytest.mark.skipif(not _has_hifloat8_backend(), reason="NPU or HiFloat8 backend is not available")` 在没有 NPU/HiFloat8 后端时直接跳过，随后第 393 行的 `x.npu()` 才会把张量放到 NPU 上。换句话说，「需要 NPU」这件事由 `skipif` 在运行期判定，而不是靠静态的 `npu` 标记。

再看覆盖率配置：

[pyproject.toml:L33-L37](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/pyproject.toml#L33-L37) — `[tool.coverage.run]` 的 `source = ["amct_pytorch"]` 把统计范围限定在产品代码，`omit = ["*/experimental/*"]` 排除实验特性目录（承接 u9-l2：experimental 是接口未稳定、默认不打包的子包，自然也不计入覆盖率考核）。

[pyproject.toml:L39-L46](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/pyproject.toml#L39-L46) — `[tool.coverage.report]` 的 `show_missing = true` 让报告显示未覆盖的行号，`exclude_lines` 列出三类「不该算未覆盖」的行：`pragma: no cover`（手工豁免）、`raise NotImplementedError`（抽象方法/占位）、`if __name__ == .__main__.:`（脚本入口）。这样覆盖率数字才不会被「本就不该被执行的行」拖低。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证三个标记「声明 vs 使用」的差距，并用 `-m` 做标记筛选。
2. **操作步骤**：
   - 在仓库根目录执行（需已安装 pytest 与 amct_pytorch 依赖）：
     ```bash
     # 统计三个标记的实际用量
     grep -rn "@pytest.mark.cpu" tests/ | wc -l
     grep -rn "@pytest.mark.npu" tests/ | wc -l
     grep -rn "@pytest.mark.slow" tests/ | wc -l
     ```
   - 只跑带 `cpu` 标记的测试：
     ```bash
     pytest tests/unit_test -m cpu -v
     ```
   - 跑「非 slow」的全部测试（即便现在没有 slow 标记，这也是 CI 常用的保护写法）：
     ```bash
     pytest tests/unit_test -m "not slow" -v
     ```
3. **需要观察的现象**：第一组命令中 `cpu` 计数远大于 `npu`/`slow`（后两者为 0）；`-m cpu` 只会执行贴了 `cpu` 标记的少量测试，数量明显少于全量。
4. **预期结果**：`cpu` 约 18 处，`npu`/`slow` 为 0；`-m cpu` 命中的用例数 ≈ 18 的函数级计数（一个 parametrize 会展开成多条）。
5. **待本地验证**：具体计数取决于当前 HEAD，若社区后续给 slow 测试补了标记，数字会变。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `markers` 里要显式声明 `cpu`/`npu`/`slow`，而不让用户随便用 `@pytest.mark.xxx`？

> **答案**：pytest 默认遇到未声明的标记会发 `PytestUnknownMarkWarning`。显式声明既消除告警，也是项目对「存在哪些测试类别」的文档化约定，方便 CI 用 `-m` 稳定筛选。

**练习 2**：`env = ["TORCH_DEVICE_BACKEND_AUTOLOAD=0"]` 想避免什么问题？

> **答案**：避免 torch 在导入时自动加载已安装的后端（如 torch_npu）。在没有 NPU 的 CPU CI 机器上，自动加载 torch_npu 可能因找不到设备而报错或拖慢启动；关掉自动加载让单测在纯 CPU 环境稳定运行。

---

### 4.2 unit_test 测试组织与 conftest 机制

#### 4.2.1 概念说明

测试目录该怎么分？AMCT 的做法是**让测试目录镜像源码目录**：源码里有 `amct_pytorch/algorithms/`，测试里就有 `tests/unit_test/algorithms/`；源码里有 `amct_pytorch/quantization/`，测试里就有 `tests/unit_test/quantization/`。这种一一对应让「改了某个模块、要找对应测试」变成机械动作，不用搜索。

`conftest.py` 是 pytest 的特殊文件，它定义 **fixture**（测试的「前置准备」资源）和 **钩子**。fixture 可以被测试函数按参数名注入；而 `autouse=True` 的 fixture 更进一步——**不需要测试函数显式声明，每条测试都会自动触发**。AMCT 用这个机制做了一件很重要的事：把随机性固定下来，让测试可复现。

#### 4.2.2 核心流程

AMCT 单元测试的执行流（由 conftest 串联）：

```
每条 test_xxx 运行前：
  ① _deterministic(autouse)         → random/np/torch 三库种子全部置 0
  （若用到临时目录）
  ② _safetensors_tmp_cleanup(session) → 会话级创建共享 tmp/ 目录
每条 test_xxx 运行后：
  ③ _deterministic 的 yield 之后      → （函数级，无额外清理）
会话全部结束：
  ④ _safetensors_tmp_cleanup 收尾     → 删除 tmp/ 目录
```

`autouse` + `scope` 的组合决定了 fixture「多久跑一次」：`scope="session"` 整个测试会话只跑一次（建目录/删目录），`scope="function"`（默认）每条测试都跑（重置种子）。这套机制让所有测试共享「种子为 0、有干净临时目录」的环境，而测试代码本身完全不用写这些样板。

#### 4.2.3 源码精读

先看目录组织。`tests/unit_test/` 下按源码模块分包（节选）：

```
tests/unit_test/
├── conftest.py                          # 根级共享 fixture
├── algorithms/        ← 对应 amct_pytorch/algorithms/（算法，承接 u6）
│   ├── test_awq.py
│   ├── test_auto_clip.py
│   ├── test_flatquant.py
│   └── test_quant_algorithm_base.py     # 用 ALGO_REGISTRY.list_all() 遍历（承接 u6-l2）
├── quantization/      ← 对应 amct_pytorch/quantization/
│   ├── test_bit_policy.py               # 承接 u3-l4 BitPolicy
│   ├── test_dtypes.py
│   └── modules/test_quant_linear.py
├── workflows/         ← 对应 amct_pytorch/workflows/
│   ├── test_llm_deploy.py               # 承接 u4-l4 deploy
│   ├── test_llm_ptq.py
│   └── test_llm_eval.py
├── common/            ← 对应 amct_pytorch/common/
│   ├── datasets/test_ptq_io.py
│   ├── optimization/test_blockwise_solver.py
│   └── models/llm/...                   # 各模型适配器测试
├── classic/           ← 对应 amct_pytorch/classic/
├── cli/               ← CLI 入口测试
├── test_packaging.py  # 顶层：打包完整性
└── test_public_api.py # 顶层：公共 API 是否可导入
```

这种镜像结构直接呼应了前面各讲：测 BitPolicy 在 `quantization/`（u3-l4）、测算法在 `algorithms/`（u6）、测 deploy 在 `workflows/`（u4-l4）。

接着看根 conftest 如何统一环境：

[tests/unit_test/conftest.py:L29-L35](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/conftest.py#L29-L35) — `_safetensors_tmp_cleanup` 是 `autouse=True, scope="session"` 的 fixture：整个测试会话开始时 `mkdir` 一个共享 `tmp/` 目录，会话结束 `yield` 之后用 `shutil.rmtree` 清掉。deploy、bit_policy 等涉及 safetensors 落盘的测试都往这里写中间产物。

[tests/unit_test/conftest.py:L38-L44](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/conftest.py#L38-L44) — `_deterministic` 是 `autouse=True`（默认 function 级）的 fixture：**每条测试运行前**把 `random`、`numpy`、`torch` 三家的随机种子都设为 0。这是量化测试可复现的关键——量化算法大量用随机权重/激活，种子固定后，`torch.randn` 产出的张量在每次跑都一样，断言才稳定。

[tests/unit_test/conftest.py:L47-L49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/conftest.py#L47-L49) — `cpu_device` 是一个普通 fixture（非 autouse），测试函数只要声明 `def test_x(cpu_device)` 就能拿到 `torch.device("cpu")`，把「在哪个设备上跑」也参数化。

现在用 `test_awq.py` 作为「一篇算法测试如何写」的样本。它的核心技巧是 **monkeypatch + 假函数**：把昂贵的真正量化 `quant_dequant_tensor` 替换成一个只记录调用的假函数，从而把被测函数（如 `process_weights_for_layers`）孤立出来单独验证逻辑。

[tests/unit_test/algorithms/test_awq.py:L76-L99](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_awq.py#L76-L99) — `test_process_weights_for_layers_int8` 用 `monkeypatch.setattr("...awq.quant_dequant_tensor", fake_qdq)` 把真量化换掉，`fake_qdq` 把每次调用的 `(wts_type, group_size)` 记进列表；测试随后断言「只被调用 1 次」且 `wts_type == "int8"`。它验证的是「流程编排正确」，而非「量化数值正确」。

[tests/unit_test/algorithms/test_awq.py:L58-L73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_awq.py#L58-L73) — `test_apply_scale_updates_weight_and_input` 验证 AWQ 的数学不变量（承接 u6-l3）：`apply_scale` 必须把权重乘 `s`、把输入除 `s`，用 `torch.allclose` 断言 `weight == weight_before * 2.0` 且 `input == input_before / 2.0`。这是「等价缩放」契约的直接测试化。

[tests/unit_test/algorithms/test_awq.py:L173-L179](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/unit_test/algorithms/test_awq.py#L173-L179) — `test_search_scale_rejects_nan_input` 测边界：输入含 NaN 时必须抛 `RuntimeError` 且消息匹配 `"Invalid value.*activation"`。`pytest.raises(..., match=...)` 同时验证异常类型与消息，是测错误路径的标准写法。

#### 4.2.4 代码实践

1. **实践目标**：跟踪一个 AWQ 测试如何隔离被测函数、覆盖边界，并回答实践任务里的问题。
2. **操作步骤**：
   - 打开 `tests/unit_test/algorithms/test_awq.py`，定位 `test_search_scale_grid_returns_best_scale`（第 128 行起）。
   - 对照 `amct_pytorch/algorithms/quant/awq.py` 里 `search_scale` 的实现（承接 u6-l3），梳理这个测试构造了什么：一个假的 `block.forward`（第一次返回 `x*2` 当作原始输出，之后返回 `x*2.1` 当作量化输出）、`grids_num=3`、`monkeypatch` 掉 `quant_dequant_tensor`。
   - 在仓库根目录执行（需已装依赖）：
     ```bash
     pytest tests/unit_test/algorithms/test_awq.py -v
     ```
3. **需要观察的现象**：测试通过；`test_search_scale_grid_returns_best_scale` 断言 `len(quant_out_calls) == 3`，说明 `search_scale` 确实按 `grids_num` 做了 3 次网格搜索试量化。
4. **预期结果**：`test_awq.py` 全部用例通过；`quant_out_calls` 为 3 验证了网格搜索次数。
5. **关于「用了哪些标记、测了哪些边界」**：`test_awq.py` **没有**用 `@pytest.mark.cpu/npu/slow` 中的任何一个——它依赖 conftest 的 `autouse` fixture（种子固定）保证可复现，本身是纯 CPU 快测，故按「cpu 是默认档」的约定无需显式标记。它覆盖的边界有：对称/非对称量化的 scale/offset 形状、AWQ 等价缩放（权重乘、输入除）、int8 与 mxfp4 两条 `process_weights_for_layers` 路径、网格搜索次数、NaN 输入/NaN 权重/NaN 损失三种错误输入、block 输出为 tuple 的兼容性。

#### 4.2.5 小练习与答案

**练习 1**：`_deterministic` 为什么是 `autouse=True` 而不是让每个测试自己 `torch.manual_seed(0)`？

> **答案**：`autouse=True` 让 fixture 对所有测试自动生效，测试代码完全不用写种子设置样板。这样既保证全仓库测试可复现，又不会出现「某个测试忘写种子导致偶发失败」。它的 `yield` 之后没有额外动作，是因为固定种子不需要「还原」。

**练习 2**：`test_awq.py` 用 `monkeypatch` 换掉 `quant_dequant_tensor` 而不是直接调真量化，得失是什么？

> **答案**：得是被测函数（`process_weights_for_layers`/`search_scale`）被孤立，测试只验证「编排逻辑」（调几次、传什么 `wts_type`），不依赖真量化的数值正确性，快且稳定；失是「真量化本身对不对」要由 `quantization/` 下的别的测试（如 `test_dtypes.py`）来守，分工明确。

---

### 4.3 pre-commit 代码规范：ruff 与 clang-format

#### 4.3.1 概念说明

AMCT 同时包含 Python（`amct_pytorch`）和 C++（`amct_ops` 的 Ascend C kernel、`amct_pytorch/classic` 的部分绑定），所以代码规范要同时管两种语言。`pre-commit` 框架把多个语言的检查器统一起来：每个 `repo` 是一个钩子来源，`hooks` 列出要跑的检查，提交时按顺序执行，失败则拒绝提交。

- **ruff**：用 Rust 写的超快 Python linter + formatter，一个工具同时替代 flake8/isort/black 的部分职能。AMCT 用它的两个钩子：`ruff-check`（查代码问题并 `--fix` 自动修）和 `ruff-format`（格式化）。
- **clang-format**：LLVM 项目的 C/C++ 格式化工具，这里固定 v16。

#### 4.3.2 核心流程

pre-commit 在 `git commit` 时的拦截链路：

```
git commit 触发 pre-commit 钩子
  → 按配置顺序对「本次暂存(staged)的文件」跑：
     1. clang-format   对 .c/.cpp 文件格式化
     2. ruff-check --fix  对 .py 文件查问题并自动修
     3. ruff-format       对 .py 文件格式化
  → 任一钩子修改了文件或检查失败 → 退出非 0 → 提交被拒绝
  → 开发者重新 git add 被自动修复的文件，再次 commit
```

注意：钩子默认只对**本次 staged 的文件**跑（`types`/`types_or` 过滤文件类型），而不是全仓库，所以提交速度很快。`ruff-check` 带 `--fix` 意味着能自动修的问题（如未用 import）会直接被改掉，但改动需要你重新 `git add`。

#### 4.3.3 源码精读

[.pre-commit-config.yaml:L3-L7](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/.pre-commit-config.yaml#L3-L7) — 第一个钩子是 `mirrors-clang-format` v16.0.0，`id: clang-format`，`types_or: [c++, c]` 限定只对 C/C++ 源码生效。这一层守护 `amct_ops/` 下的 kernel 与 binding 代码风格。

[.pre-commit-config.yaml:L10-L17](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/.pre-commit-config.yaml#L10-L17) — 第二个 `repo` 是 `ruff-pre-commit` v0.14.14，含两个钩子：`ruff-check` 带 `args: ["--output-format", "github", "--fix"]`（输出 github 风格、自动修复），`types: [python]` 只查 Python；`ruff-format` 负责 Python 格式化。ruff 的格式化风格反过来由 `pyproject.toml` 约束：

[pyproject.toml:L48-L49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/pyproject.toml#L48-L49) — `[tool.ruff.format]` 的 `quote-style = "preserve"` 告诉 ruff-format「保留原文件已有的引号风格（单引号/双引号）而不强制统一」。这是一个偏保守的选择：避免一次格式化把全文件引号刷一遍、造成大 diff。

#### 4.3.4 代码实践

1. **实践目标**：在本地把 pre-commit 跑起来，观察它如何拦截不规范代码。
2. **操作步骤**：
   - 安装并启用钩子（一次性）：
     ```bash
     pip install pre-commit
     pre-commit install        # 把钩子写入 .git/hooks/pre-commit
     ```
   - 手动对全仓库跑一次（不依赖 commit）：
     ```bash
     pre-commit run --all-files
     ```
   - 故意制造一个问题验证拦截：在某 `.py` 文件加一行未使用的 import，`git add` 后 `git commit`，观察提交是否被拒绝。
3. **需要观察的现象**：`ruff-check` 会报告并尝试 `--fix` 移除未用 import；若它修改了文件，commit 退出非 0；重新 `git add` 后才能提交成功。
4. **预期结果**：`pre-commit run --all-files` 对干净仓库应全绿；制造的问题会被 ruff 拦下。
5. **待本地验证**：未安装 pre-commit 或无网络拉取钩子仓库时，`pre-commit install` 可能失败，需可访问 `gitcode.com/gh_mirrors/ru/ruff-pre-commit`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ruff-check` 用 `--output-format github`？

> **答案**：`github` 格式输出形如 `file:line:col: code message`，能被 GitCode/GitHub 的 PR 界面解析成可点击的行内注释，方便在 MR 评论里直接定位问题。

**练习 2**：`quote-style = "preserve"` 相比强制双引号，好处和坏处各是什么？

> **答案**：好处是减少无意义的引号翻转 diff，让 review 聚焦真实改动；坏处是仓库内引号风格不完全统一。AMCT 选了「少 diff」优先。

---

### 4.4 GitCode CI 流水线编排

#### 4.4.1 概念说明

AMCT 托管在 GitCode（华为代码托管平台），CI 配置在 `.gitcode/workflows/` 而非 `.github/workflows/`。流水线语法与 GitHub Actions 相似：用 `on` 触发、`stages`/`jobs` 组织、`uses` 调用可复用子流水线或 action。

主流水线 `amct_action.yml`（名字 `PR-pipeline_amct`）把一次合并请求（PR）的全套校验编排成 **四个 stage**，每个 stage 内的 job 并行、stage 之间有依赖。本讲关注的「单测 + 覆盖率」在 stage3「ut」；pre-commit 则在 stage2 的 `codecheck` 里。

#### 4.4.2 核心流程

PR 流水线的四阶段（带关键产物）：

```
PR 触发（评论 /compile 或 workflow_dispatch）
 └ stage1 PreBuild   → 产出镜像版本号、PR 文件清单、预检
 └ stage2 compile    → 并行编译多 arch 产物（x86/arm × 普通/torch × ubuntu20/24）
                       + codecheck（含 precommit 跑 pre-commit）+ staticcheck md_check
 └ stage3 ut         → api_check + ut_action（跑 ut.sh + ut-cov-report 覆盖率）
 └ stage4 PreSmoke   → 仅 master 分支：A2 NPU 上跑 pre_smoke.sh 冒烟
 └ post              → query-pipeline 汇总结果、回写 PR 标签
```

stage3 的 ut 是本讲核心：CI 拉起一个预装 CANN 的容器镜像，下载 PR 改动清单与 `ut.sh`，执行 `bash ut.sh` 跑测试，再用 `ut-cov-report` action 把覆盖率结果上报并打包成 `ut_cov_*.tar.gz` 上传。`fail-fast` 在 ut 阶段设为 `false`，意味着即使某个 job 失败，其它 job 仍会跑完——方便一次看到所有问题。

#### 4.4.3 源码精读

[.gitcode/workflows/amct_action.yml:L31-L40](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/.gitcode/workflows/amct_action.yml#L31-L40) — `stages:` 定义四段流水线，`stage1` 名为 PreBuild。注意顶部 `concurrency: max: 5` 限制同一 PR 最多 5 个并发流水线，避免资源浪费。

[.gitcode/workflows/amct_action.yml:L214-L240](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/.gitcode/workflows/amct_action.yml#L214-L240) — `stage3` 名为 ut，含 `JOB_api_check`（仅 master 分支跑 API 兼容性检查）与 `JOB_ut`（调用 `ut_action.yml` 子流水线）。`pre: - type: auto` + `fail-fast: false` 让本阶段 job 间不互相拖垮。

[.gitcode/workflows/amct_action.yml:L199-L213](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/.gitcode/workflows/amct_action.yml#L199-L213) — stage2 的 `JOB_codecheck` 调用 `codecheck_action.yml`，与编译 job 并行；它是 pre-commit 在 CI 侧的落点。

再看 ut 子流水线如何跑覆盖率：

[.gitcode/workflows/ut_action.yml:L40-L57](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/.gitcode/workflows/ut_action.yml#L40-L57) — `ut_acc` 步骤执行 `bash ut.sh`（实际测试命令封装在 `ut.sh`，由 CI 下载），`ut_cov` 步骤用 `cann/.gitcode/actions/ut-cov-report@master` 把上一步的 `ut_process` 输出转成覆盖率报告，`language: "python"`、`ut_type: amct`。`upload` 步骤把 `ut_cov_*.tar.gz` 传到 OBS（对象存储）归档。

[.gitcode/workflows/codecheck_action.yml:L34-L42](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/.gitcode/workflows/codecheck_action.yml#L34-L42) — `precommit` 步骤用 `cann/.gitcode/actions/precommit@master` 跑 `.pre-commit-config.yaml`，即把 4.3 节的 ruff/clang-format 在 CI 上强制执行一遍；`if: ${{ always() }}` 的 upload 保证即便 pre-commit 失败也能上传日志供排查。

把这条线串起来：**开发者本地 commit → pre-commit 拦截（4.3）→ 推送 PR → CI stage2 codecheck 再跑一遍 pre-commit + stage3 ut 跑 pytest + 覆盖率（4.1/4.2）**。同一套 `pyproject.toml` 与 `.pre-commit-config.yaml` 在本地与 CI 两侧共用，保证「本地过了、CI 也过」。

#### 4.4.4 代码实践

1. **实践目标**：读主流水线，画出四个 stage 及其包含的 job，回答「单测在哪个 stage 跑、覆盖率如何上报」。
2. **操作步骤**：
   - 打开 `.gitcode/workflows/amct_action.yml`，依次定位 `stage1`/`stage2`/`stage3`/`stage4` 的 `name` 与各自 `jobs`。
   - 打开 `.gitcode/workflows/ut_action.yml`，找到执行 `bash ut.sh` 的步骤名（`ut_acc`）与上报覆盖率的步骤名（`ut_cov`）。
   - 用一句话标注：哪些 stage 是 `fail-fast: true`（出错即终止后续），哪些是 `false`。
3. **需要观察的现象**：stage1、stage4 是 `fail-fast: true`（前置与环境检查、冒烟需尽早暴露问题），stage2、stage3 是 `fail-fast: false`（编译/测试想一次看全所有 arch 与用例结果）。
4. **预期结果**：四个 stage 分别为 PreBuild / compile / ut / PreSmoke；单测与覆盖率在 stage3「ut」，由 `ut_action.yml` 的 `ut_acc`（跑 `ut.sh`）与 `ut_cov`（`ut-cov-report`）两步完成。
5. **待本地验证**：`ut.sh` 由 CI 从 OBS 下载，不在仓库内，本地无法直接查看其内容；如需复现 CI 的精确 pytest 命令，需待本地验证或参考 CI 日志。

#### 4.4.5 小练习与答案

**练习 1**：为什么 stage4（PreSmoke）带 `if: env.TARGET_BRANCH == 'master'`，而 stage3（ut）不带？

> **答案**：PreSmoke 要占用真实 A2 NPU 机器跑冒烟（资源昂贵），只在合入 master 这种高风险场景才跑；单测是纯 CPU、便宜，每个 PR 都该跑，所以不设分支限制。

**练习 2**：pre-commit 在本地已经能拦，为什么 CI 的 codecheck 还要再跑一遍？

> **答案**：并非所有开发者都装了 pre-commit（`pre-commit install` 是可选的），或可能用 `--no-verify` 绕过。CI 再跑一遍是「不可绕过」的兜底，保证入库代码一定符合规范——本地是「方便」，CI 是「强制」。

---

## 5. 综合实践

把本讲四个模块串成一个任务：**给一个已存在的模块补一条单元测试，让它从本地到 CI 全链路打通**。

背景：承接 u3-l4，`BitPolicy.cache_bits(key)` 在 key 缺失时返回默认 16。现假设你想加一条「同时配置 q=8、k=4，验证两者互不影响」的测试。

1. **定位与阅读**：打开 `tests/unit_test/quantization/test_bit_policy.py`，找到 `test_cache_bits_returns_configured_value`（第 238 行附近），读懂它的断言风格。
2. **新增测试**：在同文件追加（示例代码，非项目原有代码）：
   ```python
   def test_cache_bits_independent_q_and_k():
       bp = BitPolicy({"attn-cache": {"q": 8, "k": 4}})
       assert bp.cache_bits("q") == 8
       assert bp.cache_bits("k") == 4
       assert bp.cache_bits("v") == 16   # 未配置的 key 仍回退 16
   ```
3. **本地验证**：
   ```bash
   pytest tests/unit_test/quantization/test_bit_policy.py::test_cache_bits_independent_q_and_k -v
   ```
   预期通过；因为 `_deterministic` autouse fixture 生效，该测试不依赖随机性，天然稳定。
4. **规范校验**：
   ```bash
   pre-commit run --files tests/unit_test/quantization/test_bit_policy.py
   ```
   预期 ruff 不报错（引号风格由 `quote-style = "preserve"` 保留）。
5. **标记与 CI 归属**：该测试是纯 CPU 快测，按本讲 4.2.3 的约定**无需**贴 `@pytest.mark.cpu`（cpu 是默认档）；它会被 stage3「ut」的 `bash ut.sh` 收集执行。请用一句话写出：它将出现在 CI 的哪个 stage、由哪个子流水线跑、覆盖率会计入哪个 `source`。
6. **参考答案**：它会出现在 **stage3「ut」**，由 **`ut_action.yml`** 的 `ut_acc`（`bash ut.sh`）执行、`ut_cov` 上报覆盖率；因为测试 `amct_pytorch.quantization.bit_policy` 属于 `[tool.coverage.run].source=["amct_pytorch"]` 且不在 `*/experimental/*` 排除范围内，所以其覆盖的行会被计入覆盖率统计。

> 提示：若本地未安装完整 amct_pytorch 依赖，第 3、4 步可能失败，可退化为「源码阅读型实践」——仅完成第 1、2、5 步并在纸上写出预期。

## 6. 本讲小结

- AMCT 的测试与规范配置集中在 `pyproject.toml`：`[tool.pytest.ini_options]` 管 pytest（两个 testpaths、三个 markers、`-ra`、关闭 torch 后端自动加载），`[tool.coverage.*]` 管 coverage（source 限 `amct_pytorch`、排除 `experimental`）。
- 三个标记 `cpu`/`npu`/`slow` 是「声明 vs 使用」的典型：实际只有 `cpu` 被应用约 18 处，`npu`/`slow` 主要为 `-m` 选择预留；真正需要 NPU 的测试靠 `skipif(torch.npu.is_available())` 守卫。
- `tests/unit_test/` 目录镜像源码结构（algorithms/quantization/workflows/common/classic/cli），`conftest.py` 的两个 `autouse` fixture 统一了「随机种子置 0」与「临时目录清理」，测试代码本身不写样板。
- 算法测试（如 `test_awq.py`）的核心技巧是 `monkeypatch` 换掉重函数、孤立被测逻辑，并用 `pytest.raises` 覆盖 NaN 等错误边界；它不贴标记，靠 conftest fixture 保可复现。
- 代码规范由 `.pre-commit-config.yaml` 守护：clang-format（C++）+ ruff-check/ruff-format（Python），`quote-style="preserve"` 减少 diff；这套配置在 CI 的 `codecheck_action.yml` 里被强制再跑一遍。
- CI 在 `.gitcode/workflows/`（GitCode 平台），主流水线 `amct_action.yml` 分 PreBuild→compile→ut→PreSmoke 四 stage；单测与覆盖率在 stage3 的 `ut_action.yml`（`bash ut.sh` + `ut-cov-report`）。

## 7. 下一步学习建议

本讲是质量保障视角的收口。建议接下来的学习方向：

1. **横向打通测试与各模块**：挑一个前面讲过但还没读测试的模块（如 u4-l3 的 `BlockwiseSolver`），去 `tests/unit_test/common/optimization/test_blockwise_solver.py` 看它如何 mock DataLoader、如何验证「只训练算法参数」。这是把「源码讲义」与「测试讲义」配对阅读的最佳方式。
2. **从读测试到写测试**：按第 5 节综合实践的方法，给 u6 讲过的某个算法（如 FlatQuant）补一条边界测试，跑通本地 pytest 与 pre-commit，理解「一个改动从本地到 CI」的完整旅途。
3. **深入 CI 与打包的关系**：结合 u1-l2 的 `build.sh`/`setup.py`，对照 `.gitcode/workflows/compile_action.yml`，看 CI 在多 arch（x86/arm）、多系统（ubuntu20/24）上如何产出分发包——这会把「构建讲义」与「CI 讲义」连起来。
4. **若负责 NPU 算子**：参考 `tests/amct_ops/test_hifloat8_cast.py`、`test_svd_quant.py`，看 amct_ops 算子如何在真实 NPU 上做 smoke 测试（承接 u8），并理解它们为何必须带 NPU 守卫。
