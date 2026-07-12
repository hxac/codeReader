# 测试体系与运行方式

## 1. 本讲目标

本讲关注 lmdeploy「怎么验证自己写得对」。读完后你应当能够：

1. 看懂 `tests/test_lmdeploy/` 的目录组织与按功能分类的规律，知道某类测试在哪个子目录。
2. 掌握用 `pytest` 定向运行单文件、单目录、单个用例、单个参数组合的命令。
3. 读得懂三个代表性测试文件——`test_messages.py`（纯单元测试）、`test_model.py`（参数化对照测试）、`test_pipeline.py`（端到端集成测试）——并理解它们各自验证了什么行为。
4. 能区分「离线可跑」与「需要 GPU + 联网下载模型」的两类测试，避免在没有资源的机器上盲目运行整组用例。

## 2. 前置知识

在进入测试源码前，先用三句话铺垫几个 pytest 概念，零基础读者也能跟上。

- **测试函数与测试类**：pytest 默认收集所有 `test_*.py` 文件里以 `test_` 开头的函数，以及以 `Test` 开头、不含 `__init__` 的类里以 `test_` 开头的方法。函数名/方法名本身就是「测试用例」。
- **参数化（`@pytest.mark.parametrize`）**：把同一份测试逻辑套到多组输入上，每组都会被当作一条独立用例。lmdeploy 大量用它来让一个测试跨几十个真实模型跑一遍。
- **fixture**：测试的「前置准备 + 后置清理」函数。被 `@pytest.fixture` 装饰，用 `yield` 把准备好的资源交给测试用例，`yield` 之后的代码在用例结束后执行（比如关引擎、回收显存）。`scope='class'` 表示整个测试类只准备一次，多个用例共享。
- **断言（`assert`）**：`assert 条件` 不成立就抛 `AssertionError`，该用例判为失败。pytest 会对 `assert` 做改写，失败时打印出参与比较的中间值，方便定位。

lmdeploy 的测试既覆盖「不需要 GPU、只测纯函数与字符串拼接」的轻量逻辑，也覆盖「需要加载真实模型并前向推理」的重量级集成测试。前者快、可离线，后者慢、依赖硬件——这是本讲反复强调的一条分界线。

## 3. 本讲源码地图

本讲聚焦三个测试文件，并交叉引用它们所「测」的目标源码：

| 文件 | 角色 | 是否需 GPU/联网 |
| --- | --- | --- |
| `tests/test_lmdeploy/test_messages.py` | 纯函数级单元测试，验证 `GenerationConfig` 等用户面类型 | 部分需联网下载模型 |
| `tests/test_lmdeploy/test_model.py` | 参数化对照测试，验证 chat 模板产出与 transformers 官方一致 | 需联网下载模型/tokenizer |
| `tests/test_lmdeploy/test_pipeline.py` | 端到端集成测试，跨 PyTorch/TurboMind 双后端跑推理 | 需 GPU + 联网 |
| `lmdeploy/messages.py`（被测对象） | `GenerationConfig` 定义与校验逻辑 | — |
| `lmdeploy/model.py`（被测对象） | `MODELS` chat 模板注册表 | — |
| `lmdeploy/serve/openai/protocol.py`（被测对象） | `ChatCompletionRequest` Pydantic 模型 | — |
| `lmdeploy/utils.py`（被测对象） | `get_hf_gen_cfg` 读取 HF generation 配置 | — |

此外，`pyproject.toml` 是构建与 lint 配置入口，本讲用它说明「lmdeploy 没有自定义 pytest 配置」这一事实。

## 4. 核心概念与源码讲解

### 4.1 测试目录全景与运行方式

#### 4.1.1 概念说明

lmdeploy 的测试统一放在 `tests/test_lmdeploy/` 下，使用业界标准的 **pytest** 框架。整个目录有约 42 个 `test_*.py` 文件，按「测的是哪一块功能」分类，大致可分四组：

- **顶层测试文件**：贴近用户面公共类型的轻量测试，如 `test_messages.py`、`test_model.py`、`test_tokenizer.py`、`test_utils.py`、`test_logger.py`。
- **`test_lite/`**：量化压缩链路测试（`test_lite/test_quantization/`），对应 U7。
- **`test_vl/`**：视觉语言模型预处理测试，如 `test_hf_chat_template.py`、`test_vl_encode.py`，对应 U9。
- **`serve/`**：服务部署相关测试，下设 `anthropic/`、`openai/`（含 `chat_completions/`、`responses/`）、`parsers/` 子目录，对应 U8。

一个关键事实：`pyproject.toml` 里**没有** `[tool.pytest.ini_options]` 段，也没有 `pytest.ini`、`setup.cfg`、`tox.ini` 之类的 pytest 配置文件。这意味着 lmdeploy 完全采用 pytest 的**默认发现规则**——没有任何自定义标记（marker）、自定义测试路径或专用的命令行选项需要先注册。好处是你只要装好 pytest，命令就能直接跑。

#### 4.1.2 核心流程

pytest 的默认收集流程可以概括为：

```text
扫描命令行给的路径
  → 找到所有 test_*.py / *_test.py 文件
    → 收集其中 test_* 函数
    → 收集 Test* 类（无 __init__）的 test_* 方法
      → 对每个 @parametrize 展开成「节点（node）」
        → 运行每个节点：准备 fixture → 跑用例 → 断言 → 清理
```

每个被展开的节点都有一个**节点 ID（node id）**，形如 `文件路径::类名::方法名[参数]`，你可以用它在命令行里精确选中一条用例。

#### 4.1.3 源码精读

先确认「没有自定义 pytest 配置」这一前提——`pyproject.toml` 里只有构建系统与 ruff lint 配置：

[pyproject.toml:L1-L16](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/pyproject.toml#L1-L16)：可见 `[build-system]` 与 `[tool.ruff]` 两段，没有任何 `[tool.pytest.*]`，故全部走默认规则。

子目录的分类可由实际目录树确认：`test_vl/`、`test_lite/test_quantization/`、`serve/{anthropic,openai,parsers}/` 各司其职，与 lmdeploy 的功能子包（`vl/`、`lite/`、`serve/`）一一对应。

#### 4.1.4 代码实践

实践目标：不实际运行测试，只用 pytest 的「收集」模式列出测试用例清单，建立对收集规则与节点 ID 的直觉。

操作步骤：

1. 在仓库根目录执行收集（只列不跑）：
   ```bash
   pytest tests/test_lmdeploy --collect-only -q
   ```
2. 观察输出的节点 ID 格式，特别注意带参数化方括号的条目，例如：
   ```
   tests/test_lmdeploy/test_model.py::test_qwen3[True-Qwen/Qwen2.5-7B-Instruct]
   ```
3. 只想看某类测试有多少条时，可加路径前缀缩小范围：
   ```bash
   pytest tests/test_lmdeploy/serve --collect-only -q
   ```

预期结果：`--collect-only` 不会真正执行用例，只打印节点列表与总数；网络/GPU 都不需要。

如果无法确定运行结果，标注「待本地验证」：在没有安装 pytest 或 import 链断裂（如未安装 TurboMind 扩展）时，收集阶段也可能报 collection error，这通常提示依赖未装齐。

#### 4.1.5 小练习与答案

**练习 1**：`test_model.py` 中 `test_qwen3` 用了两个 `@parametrize`（model_path 与 enable_thinking），各 3 个取值。它一共会被展开成多少条用例节点？

**参考答案**：参数化是笛卡尔积，`3 × 3 = 9` 条节点，每条形如 `test_qwen3[<enable_thinking>-<model_path>]`。

**练习 2**：为什么 lmdeploy 可以不在 `pyproject.toml` 里写 pytest 配置？

**参考答案**：因为它只用默认发现规则（`test_*.py`、`test_*`、`Test*`），没有自定义 marker、没有改 testpaths、没有注册插件选项，因此默认配置就够用，不需要 `[tool.pytest.ini_options]`。

---

### 4.2 test_messages：纯函数级单元测试

#### 4.2.1 概念说明

`test_messages.py` 是 lmdeploy 最轻量的测试之一，专门验证**用户面公共类型** `GenerationConfig` 与 `ChatCompletionRequest` 的「构造期校验」与「方法行为」。它测的是纯 Python 逻辑——不需要 GPU，绝大多数不需要跑模型。这类测试的价值在于：把「参数填错」这类 bug 在毫秒级暴露，而不是等到加载完几 GB 模型后才报错。

文件里有四个测试函数，分别盯防四种行为：

1. 负的 ngram 参数被夹到 0（clamping）。
2. 服务层请求模型用 Pydantic 拒绝负值。
3. `GenerationConfig` 能把字符串停止词翻译成 token id。
4. `GenerationConfig` 能从 HuggingFace 的 `generation_config.json` 同步停止 token。

#### 4.2.2 核心流程

四个测试彼此独立，可单独运行。它们共享一个套路：「构造对象 → 触发校验/方法 → 断言结果或异常」。

```text
test_generation_config_repetition_ngram_clamped
  传入负值 → 期望 __post_init__ 自动夹到 0

test_chat_completion_request_repetition_ngram_ge_zero
  传入负值 → 期望 pydantic 抛 ValidationError

test_engine_generation_config
  建 tokenizer + GenerationConfig(stop_words=[...])
    → 调 convert_stop_bad_words_to_ids
    → 断言 stop_token_ids 与手工 encode 一致

test_update_from_hf_gen_cfg（参数化 3 个模型）
  读 HF generation_config → 调 update_from_hf_gen_cfg
    → 断言 stop_token_ids 非空
```

#### 4.2.3 源码精读

[tests/test_lmdeploy/test_messages.py:L10-L13](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_messages.py#L10-L13)：`test_generation_config_repetition_ngram_clamped` 传入负的 `repetition_ngram_size` 与 `repetition_ngram_threshold`，断言构造后两者都变成了 0。它验证的是被测源码 [lmdeploy/messages.py:L203-L205](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L203-L205)：`__post_init__` 里 `if ... <= 0: ... = 0` 的夹断逻辑——即 dataclass 一构造就自愈非法输入。

[tests/test_lmdeploy/test_messages.py:L16-L22](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_messages.py#L16-L22)：`test_chat_completion_request_repetition_ngram_ge_zero` 用 `pytest.raises(ValidationError)` 断言传 `-1` 会抛错。注意这是**服务层**的请求模型，与上一个测试形成对照：同一个语义（ngram 不能为负），在 `GenerationConfig` 里是「静默夹到 0」，在 `ChatCompletionRequest` 里是「直接报错」。差异源自被测源码 [lmdeploy/serve/openai/protocol.py:L180](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/protocol.py#L180)：`repetition_ngram_size: int = Field(default=0, ge=0)` 中的 `ge=0`（大于等于 0）约束由 Pydantic 强制执行。这两个测试放在一起，恰好把「同字段、两套校验策略」钉死。

[tests/test_lmdeploy/test_messages.py:L25-L32](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_messages.py#L25-L32)：`test_engine_generation_config` 把字符串停止词 `'<|im_end|>'` 经 `convert_stop_bad_words_to_ids` 转成 id，并断言结果与手工 `tokenizer.encode` 一致且是 `list[int]`。它验证的是被测方法 [lmdeploy/messages.py:L154](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L154)。注意此用例需要联网下载 `internlm/internlm2-chat-7b` 的 tokenizer。

[tests/test_lmdeploy/test_messages.py:L35-L46](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_messages.py#L35-L46)：`test_update_from_hf_gen_cfg` 参数化三个模型，经 [lmdeploy/utils.py:L246](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/utils.py#L246) 的 `get_hf_gen_cfg` 读出 HF 官方 generation 配置，再调 [lmdeploy/messages.py:L176](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L176) 的 `update_from_hf_gen_cfg` 同步进 `GenerationConfig`，断言 `stop_token_ids` 非空。这条用例同样需要联网。

#### 4.2.4 代码实践

实践目标：分清文件内「离线可跑」与「需联网」的用例，并能用节点 ID 精确运行。

操作步骤：

1. 先只跑两个纯函数用例（无需网络、无需 GPU）：
   ```bash
   pytest tests/test_lmdeploy/test_messages.py::test_generation_config_repetition_ngram_clamped \
          tests/test_lmdeploy/test_messages.py::test_chat_completion_request_repetition_ngram_ge_zero -v
   ```
2. 需要联网下载模型的两条用例（`test_engine_generation_config`、`test_update_from_hf_gen_cfg`）请在本机有 HuggingFace 访问权限时运行：
   ```bash
   pytest tests/test_lmdeploy/test_messages.py -v
   ```

需要观察的现象：
- 离线用例应在毫秒级通过，输出两个绿点（或带 `-v` 的 `PASSED`）。
- 联网用例首次运行会触发 tokenizer/模型配置下载；若无网络会报连接超时或 `OSError`，属预期。

预期结果：四条用例全部 `PASSED`。若本机无网络/未装 lmdeploy，后两条的运行结果标注「待本地验证」。

> 说明：本讲义不在此环境内实际执行上述命令——`test_engine_generation_config` 与 `test_update_from_hf_gen_cfg` 依赖 HuggingFace 在线下载，是否通过取决于本地网络与模型可用性，请以本地实际运行为准。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `test_generation_config_repetition_ngram_clamped` 里的断言去掉，仅保留构造语句，这个测试还能「防回归」吗？

**参考答案**：不能。没有 `assert`，pytest 只要不抛异常就算通过；而构造 `GenerationConfig(负值)` 恰恰不抛异常（它被夹到 0），测试会永远「绿」却什么都没验证。断言是测试的真正核心。

**练习 2**：为什么 ngram 校验在 `GenerationConfig` 与 `ChatCompletionRequest` 上表现不同（一个夹断、一个报错）？结合被测源码说明。

**参考答案**：`GenerationConfig`（[messages.py:L203-L205](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L203-L205)）走 `__post_init__` 的容错夹断，偏向「尽量可用」；`ChatCompletionRequest`（[protocol.py:L180](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/protocol.py#L180)）用 Pydantic 的 `Field(ge=0)`，偏向「对外接口严格报错」。前者是内部推理配置，后者是 HTTP 对外契约，故策略不同。

---

### 4.3 test_model：chat 模板的参数化对照测试

#### 4.3.1 概念说明

`test_model.py` 验证的是 lmdeploy 的 **chat 模板**——把多轮对话拼成模型输入文本的逻辑（详见 u2-l4）。它的核心思路是「**对照测试**」：拿 HuggingFace `transformers` 官方的 `apply_chat_template` 当「标准答案」，断言 lmdeploy 自己的 `HFChatTemplate` 产出的字符串与之**逐字符相等**。

这个文件大量使用参数化：一个名为 `HF_MODELS_WITH_CHAT_TEMPLATES` 的列表列出约 30 个真实模型，让同一份测试逻辑跨所有模型跑一遍。任何一个模型的模板实现一旦偏离官方行为，对应节点立刻失败。此外还有针对内置模板（base/vicuna/llama2/codellama）的非对照单测。

#### 4.3.2 核心流程

对照测试的固定四步套路：

```text
1. MODELS.get('hf')(model_path=...) 构造 lmdeploy 模板对象
2. AutoTokenizer.apply_chat_template(messages, ...) 产出「官方答案」expected
3. model.get_prompt(...) / model.messages2prompt(...) 产出「lmdeploy 结果」actual
4. assert actual == expected
```

这套流程对每个模型重复一次，靠 `@parametrize` 自动展开。对内置模板的单测则不走对照，而是直接断言拼接结果里的某些不变量（如 system 文本是否出现、stop_words 是否正确）。

#### 4.3.3 源码精读

[tests/test_lmdeploy/test_model.py:L5-L55](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_model.py#L5-L55)：`HF_MODELS_WITH_CHAT_TEMPLATES` 是参数化数据源，覆盖 Qwen、InternLM、InternVL、DeepSeek、GLM、Phi、Yi 等模型族。注意末尾注释：部分需鉴权的模型被注释掉，说明参数化列表本身就是「支持范围」的活文档。

[tests/test_lmdeploy/test_model.py:L58-L67](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_model.py#L58-L67)：`test_HFChatTemplate_get_prompt_sequence_start_True` 是对照测试的范本——`MODELS.get('hf')` 从注册表 [lmdeploy/model.py:L13](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/model.py#L13) 取出 HF 模板，断言其 `get_prompt` 与官方 `apply_chat_template` 完全一致。

[tests/test_lmdeploy/test_model.py:L83-L87](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_model.py#L83-L87)：`test_base_model` 测内置 `base` 模板——`capability='completion'` 时原样返回文本（`get_prompt('hi') == 'hi'`），验证「最简模板什么都不加」这一基线行为。

[tests/test_lmdeploy/test_model.py:L90-L105](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_model.py#L90-L105)：`test_vicuna` 展示了对 `capability` 的分支测试——`completion` 模式不加系统前缀（结果等于原文），`chat` 模式加前缀（结果不等于原文），而非法 `voice` 能力用 `pytest.raises(AssertionError)` 断言抛错。

[tests/test_lmdeploy/test_model.py:L215-L244](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_model.py#L215-L244)：`test_qwen3` 是双参数化（3 模型 × 3 个 `enable_thinking` 取值）的对照测试，验证 Qwen3 的思考模式开关与官方 tokenizer 行为一致。

#### 4.3.4 代码实践

实践目标：学会用「节点 ID」精确选中一条参数化用例，避免每次都跑全表。

操作步骤：

1. 只跑不依赖网络/模型下载的纯内置模板用例：
   ```bash
   pytest tests/test_lmdeploy/test_model.py::test_base_model \
          tests/test_lmdeploy/test_model.py::test_vicuna -v
   ```
2. 选中对照测试的某一个参数组合（节点 ID 用方括号写参数），例如只测 Qwen3 + `enable_thinking=True`：
   ```bash
   pytest "tests/test_lmdeploy/test_model.py::test_qwen3[True-Qwen/Qwen3-8B]" -v
   ```
3. 用 `-k` 按名字模糊过滤，例如只跑名字含 `qwen3` 的用例：
   ```bash
   pytest tests/test_lmdeploy/test_model.py -k qwen3 -v
   ```

需要观察的现象：内置模板用例离线即可通过；对照测试用例（`test_HFChatTemplate_*`、`test_qwen3` 等）会触发 `AutoTokenizer.from_pretrained` 下载对应模型配置。

预期结果：所选节点 `PASSED`；若本机无网络，对照类用例的运行结果标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：对照测试为什么用 `==` 比较整串文本，而不是只检查某些关键字是否存在？

**参考答案**：chat 模板的正确性恰恰在细节——多一个换行、少一个特殊标记都会改变模型实际收到的输入，进而影响生成。整串 `==` 能捕捉这些细微差异；只查关键字会漏掉格式 bug，测试会「假绿」。

**练习 2**：`-k qwen3` 与直接写节点 ID `test_qwen3[True-...]` 有何取舍？

**参考答案**：`-k` 按用例名子串过滤，方便快速圈定一组相关用例，但会命中所有名字含 `qwen3` 的节点（含多个参数组合）；精确节点 ID 只跑唯一一条，最省时间，适合调试单个失败用例。

---

### 4.4 test_pipeline：端到端推理集成测试

#### 4.4.1 概念说明

`test_pipeline.py` 是 lmdeploy 最「重」的测试——它真正加载模型、跑前向、返回 `Response`，属于**端到端集成测试**。因此它要求 GPU 与联网下载模型，不在 CI 的常规无 GPU 环境里跑。

它的设计亮点是用 **`TestBackendInference` 测试类 + 参数化后端**，让同一整套用例（infer、stream_infer、chat、get_ppl、session 多轮）在 `pytorch` 与 `turbomind` **两个后端**上各跑一遍，从而保证用户面 `Pipeline` 接口在双后端下行为一致。这一点呼应了 u3-l1 讲过的「两条后端、一个 Pipeline」架构主线。

#### 4.4.2 核心流程

文件用两个 `scope='class'` 的 fixture 把「最贵的资源」——pipeline 实例——在整类范围内只建一次：

```text
TestBackendInference（parametrize backend ∈ {pytorch, turbomind}）
  ├─ fixture backend_config(backend)
  │     按 backend 返回 PytorchEngineConfig 或 TurbomindEngineConfig
  ├─ fixture pipe(backend_config)   # scope='class', autouse
  │     pipeline(MODEL_ID, backend_config=...)
  │     yield pipe
  │     pipe.close(); gc.collect(); torch.cuda 清理   # 后置
  └─ 一组 test_* 方法，全部以 pipe 为入参
        test_infer_single_string / test_infer_batch_strings / ...
        test_stream_infer_single / test_chat_streaming / test_get_ppl_*
```

`autouse=True` 让这两个 fixture 自动生效于类内每个用例，`scope='class'` 保证一个后端只加载一次模型——否则每个用例都重新建 pipeline、下载模型，测试会慢到不可用。

#### 4.4.3 源码精读

[tests/test_lmdeploy/test_pipeline.py:L9-L13](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py#L9-L13)：`MODEL_ID = 'Qwen/Qwen3-8B'` 指定被测模型；`@pytest.mark.parametrize('backend', ['pytorch', 'turbomind'], scope='class')` 把后端作为类级参数，整类按后端各实例化一次。

[tests/test_lmdeploy/test_pipeline.py:L16-L25](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py#L16-L25)：`backend_config` fixture 按 backend 名字返回对应的引擎配置，二者字段几乎对称（`session_len=4096, max_batch_size=4, tp=1, cache_max_entry_count=0.1`），这正是 u2-l3 讲过的「两套引擎配置共有字段」的体现。

[tests/test_lmdeploy/test_pipeline.py:L27-L37](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py#L27-L37)：`pipe` fixture 是「建—用—拆」三段式的范本。`yield pipe` 之前建实例，之后执行 `pipe.close()`、`gc.collect()`，并在有 CUDA 时 `reset_peak_memory_stats` 与 `synchronize`——确保显存被彻底回收，避免用例间相互污染。

[tests/test_lmdeploy/test_pipeline.py:L39-L48](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py#L39-L48)：`test_infer_single_string` 验证最基础的 `pipe.infer(str)` 返回 `Response` 且文本非空——它测的是用户面 `Response`（[lmdeploy/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py)）的字段契约（`text`/`generate_token_len`/`input_token_len`）。

[tests/test_lmdeploy/test_pipeline.py:L76-L83](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py#L76-L83)：`test_infer_with_generation_config` 传入自定义 `GenerationConfig(max_new_tokens=50, ...)`，并断言 `response.generate_token_len <= 50`——把 u2-l2 讲的采样/长度参数与实际输出长度挂上钩。

[tests/test_lmdeploy/test_pipeline.py:L285-L292](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/tests/test_lmdeploy/test_pipeline.py#L285-L292)：`test_infer_zero_tokens` 是一个**边界用例**：`max_new_tokens=0` 时断言 `generate_token_len == 0`，验证「立即结束、不产出 token」这一边界行为不被回归。

#### 4.4.4 代码实践

实践目标：在**有 GPU 的本机**上定向运行某个后端的某个用例，并理解为何不能在无 GPU 环境跑。

操作步骤：

1. 仅运行 PyTorch 后端、仅单个用例（用 `-k` 同时过滤后端与方法名）：
   ```bash
   pytest tests/test_lmdeploy/test_pipeline.py -k "pytorch and test_infer_single_string" -v
   ```
2. 运行 TurboMind 后端的全部用例：
   ```bash
   pytest tests/test_lmdeploy/test_pipeline.py -k turbomind -v
   ```
3. 跑边界用例确认长度限制：
   ```bash
   pytest tests/test_lmdeploy/test_pipeline.py -k "zero_tokens" -v
   ```

需要观察的现象：首次运行会下载 `Qwen/Qwen3-8B` 权重（数 GB），并占用 1 张 GPU（`tp=1`）；fixture 会在用例结束后关闭 pipeline 并清显存。

预期结果：所选节点 `PASSED`。本讲义不在当前环境执行——该文件强依赖 GPU 与模型下载，运行结果请以本地为准，标注「待本地验证」。

> 若本机无 GPU 或未编译 TurboMind 扩展，`-k turbomind` 的用例会在 fixture 阶段就报错（无法创建 TurboMind 引擎）；这是预期行为，可用 `-k pytorch` 回避。

#### 4.4.5 小练习与答案

**练习 1**：`pipe` fixture 的 `scope='class'` 改成默认的 `scope='function'` 会带来什么后果？

**参考答案**：默认 function 级会让类内**每个** `test_*` 方法都重建一次 pipeline、重新下载/加载模型，再关闭。类里有十几个方法 × 2 个后端，意味着几十次重复加载，测试时间与显存抖动都会爆炸。`scope='class'` 让一个后端只加载一次，是该文件可用的关键。

**练习 2**：为什么 `test_infer_with_generation_config` 断言用 `<= 50` 而不是 `== 50`？

**参考答案**：`max_new_tokens` 是「最多生成」的上限，模型可能因命中停止词或 EOS 提前结束，实际生成长度可以小于 50，但绝不应超过。故合理的断言是「不超过上限」而非「严格等于」。

---

## 5. 综合实践

把本讲三件事（读测试、写测试、跑测试）串起来：

**任务**：为 `test_messages.py` 增补一条新用例，验证 `GenerationConfig` 的 `temperature` 校验行为，并用 pytest 精确定位运行它。

步骤建议：

1. **读源码**：打开 [lmdeploy/messages.py:L195-L206](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L195-L206)，确认 `__post_init__` 里 `temperature` 的合法范围与失败方式（是 `assert` 抛错，还是夹断？）。
2. **写用例**：在 `test_messages.py` 仿照现有风格新增一个测试，例如验证「`temperature` 超出 `[0,2]` 会触发断言」：
   ```python
   def test_generation_config_temperature_out_of_range():
       with pytest.raises(AssertionError):
           GenerationConfig(temperature=3.0)
   ```
   （此为示例代码，新增前请以源码实际校验逻辑为准。）
3. **定向运行**：只跑你新加的这条，避免触发联网用例：
   ```bash
   pytest tests/test_lmdeploy/test_messages.py::test_generation_config_temperature_out_of_range -v
   ```
4. **收集确认**：用 `pytest tests/test_lmdeploy/test_messages.py --collect-only -q` 确认新用例已被 pytest 发现。
5. **观察**：若源码确实对 `temperature` 做了范围断言，新用例应 `PASSED`；若你误判了校验方式（比如源码其实是夹断而非报错），测试会失败——此时根据失败信息回头修正对源码的理解。这正是「测试驱动的源码阅读」。

完成标准：能说清新用例验证了什么、为什么这么断言、以及它在命令行里的节点 ID。

## 6. 本讲小结

- lmdeploy 用 **pytest** 框架，测试集中在 `tests/test_lmdeploy/`，约 42 个 `test_*.py` 文件，按功能分顶层/`test_lite/`/`test_vl/`/`serve/` 四组，与功能子包一一对应。
- `pyproject.toml` **没有**自定义 pytest 配置，全部走默认发现规则；唯一的「marker」是 `parametrize`，无需额外注册。
- 用 `pytest 路径`、`pytest 文件`、`pytest 文件::用例[参数]`、`-k 过滤串`、`--collect-only` 可实现从粗到细的定向运行。
- `test_messages.py` 是**纯函数级单元测试**，前两条离线可跑、后两条需联网；它对照揭示了 `GenerationConfig`（夹断）与 `ChatCompletionRequest`（Pydantic 报错）对同字段的不同校验策略。
- `test_model.py` 采用**对照测试**——以 transformers 官方 `apply_chat_template` 为标准答案，参数化跨约 30 个模型逐字符比较，并辅以内置模板（base/vicuna/llama2/codellama）的不变量单测。
- `test_pipeline.py` 是**端到端集成测试**，用 `TestBackendInference` + `parametrize(['pytorch','turbomind'])` 让同一套用例跑双后端，靠 `scope='class'` 的 fixture 复用昂贵的 pipeline 实例；强依赖 GPU 与模型下载。

## 7. 下一步学习建议

- **横向扩展阅读**：用 `pytest tests/test_lmdeploy/serve --collect-only` 浏览服务层测试，结合 u8 讲义阅读 `serve/openai/chat_completions/` 下的测试，看协议层如何被验证。
- **纵向深入量化测试**：进入 `test_lite/test_quantization/`，对照 u7 讲义理解 AWQ/GPTQ/SmoothQuant 的测试断言形态（往往比较量化前后输出误差）。
- **动手实践**：按「综合实践」的流程，挑一个你最关心的 `GenerationConfig` 字段（如 `top_p`、`min_p`）补一条测试，跑通它，巩固「读源码—写断言—定向运行」的闭环。
- **CI 视角**：阅读仓库根的 GitHub Actions 配置（`.github/workflows/`），看项目在 CI 里实际跑哪些测试、如何按 GPU 可用性筛选——这是把本讲的「定向运行」推广到工程化流水线的关键。
