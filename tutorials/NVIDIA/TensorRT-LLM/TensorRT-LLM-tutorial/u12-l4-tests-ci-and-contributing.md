# 测试、CI 与贡献流程

## 1. 本讲目标

前面十一单元我们一直在「读」TensorRT-LLM——理解它的架构、调度、KV cache、MoE、量化、AutoDeploy……这一讲换个角度，讲「写」：当你修改了源码，仓库用什么机制验证它不会把别人搞坏？你又该走什么流程把改动合进 `main`？

学完本讲，你应当能够：

1. 说清 TensorRT-LLM 的**测试三层**（单元 / 集成 / API 稳定性）各自放在哪、谁来跑、解决什么问题。
2. 用 [`scripts/test_to_stage_mapping.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/scripts/test_to_stage_mapping.py) 把一个测试名反查到它会被哪个 Jenkins stage 跑，并会用 `/bot run` 系列 PR 评论去触发 CI。
3. 按仓库的**贡献规范**提交一个干净、可被快速 review 的 PR：DCO 签名、PR 标题格式、NVIDIA 版权头、pre-commit、golden manifest、API 稳定性自检。

本讲是高级篇的收尾，也是从「读者」迈向「贡献者」的临门一脚。它不强依赖任何前一讲的内部实现细节，但默认你已经读过 [u2-l1 顶层目录与代码组织](u2-l1-repo-layout.md)，知道 `tests/`、`scripts/`、`jenkins/`、`cpp/`、`tensorrt_llm/` 这些顶层目录的职责。

## 2. 前置知识

- **pytest**：Python 的事实标准测试框架。TensorRT-LLM 的单元和集成测试都用它写。一条「完全限定测试名」（fully qualified name）形如 `tests/unittest/_torch/sampler/test_beam_search.py::TestClass::test_method[param]`，其中 `::` 分隔文件/类/方法，`[...]` 是参数化用例的 id。
- **Jenkins + Groovy**：CI 服务器，`jenkins/` 下的 `.groovy` 文件用 Groovy 语法描述流水线（pipeline）。
- **pre-commit**：一个在 `git commit` 前自动跑代码格式化与静态检查（lint）的框架；仓库的 `.pre-commit-config.yaml` 声明了每个 commit 要跑哪些 hook。
- **DCO（Developer Certificate of Origin）**：开源协议合规机制。`git commit -s` 会在提交信息末尾追加一行 `Signed-off-by: ...`，声明「这段代码是我原创的、我有权以本仓库的开源协议提交」。
- **Conventional Commits**：一种提交/PR 标题约定，用 `feat:` / `fix:` / `chore:` / `perf:` 等前缀标注改动性质。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|------|------|
| `tests/unittest/` | 单元测试大本营，merge-request 流水线里跑，不需要 GPU 模型 |
| `tests/unittest/api_stability/` | API 稳定性测试套件，守护「公开 API 签名」不被悄悄改坏 |
| `tests/integration/defs/` | 集成测试用例本体，需要 GPU + 真实模型权重 |
| `tests/integration/test_lists/test-db/*.yml` | 把集成/部分单元测试**按硬件**登记、并标注 `stage`/`backend` 的清单 |
| `tests/integration/test_lists/waives.txt` | 已知失败用例的「豁免清单」，CI 自动跳过 |
| `jenkins/L0_Test.groovy` | post-merge 流水线，把「stage 名」映射到「YAML 文件 + 分片」 |
| `jenkins/L0_MergeRequest.groovy` | merge-request 流水线，跑单元测试 + `pre_merge` 集成测试 |
| `scripts/test_to_stage_mapping.py` | 本讲主角：测试名 ↔ Jenkins stage 的双向反查工具 |
| `docs/source/developer-guide/ci-overview.md` | CI 官方说明 |
| `CONTRIBUTING.md` | 贡献流程（fork、DCO、PR 标题、API 稳定性） |
| `CODING_GUIDELINES.md` | 编码规范 + pre-commit 双组 lint + NVIDIA 版权头 |
| `AGENTS.md` | 给人/AI 看的「硬规则速查」，含 PR 标题、golden manifest、bot 命令 |

## 4. 核心概念与源码讲解

### 4.1 测试分层：单元 / 集成 / API 稳定性

#### 4.1.1 概念说明

TensorRT-LLM 是一个跨 Python / C++ / CUDA 的超大型项目，光靠一种测试既不现实也不经济。仓库把验证拆成三层，按「成本」与「目的」分工：

| 层 | 位置 | 成本 | 目的 |
|----|------|------|------|
| **单元测试** | `tests/unittest/` | 低（多数不要 GPU/模型） | 验证单个模块/函数的正确性，快速反馈 |
| **集成测试** | `tests/integration/defs/` | 高（要 GPU + 真实权重） | 端到端验证「模型真的能跑、结果对」 |
| **API 稳定性测试** | `tests/unittest/api_stability/` | 低 | 守护公开 API 的签名/默认值不被悄悄改动 |

一句话区分：单元测试问「这块代码对不对」，集成测试问「这套模型端到端跑通且精度达标吗」，API 稳定性测试问「我有没有破坏用户已经在用的接口契约」。

#### 4.1.2 核心流程

测试按「何时被触发」分两条流水线：

1. **merge-request 流水线**（`jenkins/L0_MergeRequest.groovy`）：每次 `/bot run` 触发。跑全部 `tests/unittest/`，外加 YAML 清单里标了 `stage: pre_merge` 的集成测试。
2. **post-merge 流水线**（`jenkins/L0_Test.groovy`）：PR 合进 `main` 后自动跑。覆盖 YAML 清单里所有 `stage: post_merge` 的用例，遍历全部受支持的 GPU 配置（A10/A100/H100/B200/GB200…）。

```text
PR 提交 ──/bot run──▶ merge-request 流水线
                        │  tests/unittest/* (全部)
                        └─ YAML 中 stage: pre_merge 的用例
合入 main ──自动──▶ post-merge 流水线
                        │  YAML 中 stage: post_merge 的用例
                        └─ 跨所有 GPU 配置、分片运行
```

关键直觉：**「在哪个 YAML 文件里登记」+「标 pre_merge 还是 post_merge」**，共同决定一个用例何时、在什么硬件上被跑。

#### 4.1.3 源码精读

**单元测试在哪里。** CI 文档明确：单元测试在 merge-request 流水线里跑，不需要映射到具体硬件 stage——[docs/source/developer-guide/ci-overview.md:37-L39](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/developer-guide/ci-overview.md#L37-L39) 旁注说明它们由 `L0_MergeRequest.groovy` 调起、与硬件 stage 解耦。`tests/unittest/` 下按子系统分子目录（`_torch/`、`llmapi/`、`quantization/`、`visual_gen/`、`api_stability/` 等）。

**集成测试登记表长什么样。** 以 `tests/integration/test_lists/test-db/l0_a30.yml` 为例，每条用例所在的「条件块」声明了硬件通配、`stage`、`backend` 三要素：

[tests/integration/test_lists/test-db/l0_a30.yml:1-L30](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tests/integration/test_lists/test-db/l0_a30.yml#L1-L30) —— 注意 `terms.stage: pre_merge`、`terms.backend: pytorch`，下面的 `tests:` 列表里既有集成测试 `test_e2e.py::...`，也夹带了不少 `unittest/_torch/...` 单元测试（它们在 merge-request 阶段于该硬件上再跑一遍）。

> 关键词 `stage` 取值 `pre_merge` / `post_merge`，`backend` 取值 `pytorch` / `tensorrt` / `triton`。这是后续 stage 反查的两把钥匙。

**API 稳定性测试在做什么。** 它不是「跑功能」，而是用 `inspect` 把公开类（如 `LLM`、`SamplingParams`、`RequestOutput`、`CompletionOutput`）的**签名快照**与仓库里**已提交的参考快照**逐字段比对；签名一变就 fail。其核心数据结构 `ParamSnapshot` 在 [tests/unittest/api_stability/api_stability_core.py:79-L90](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tests/unittest/api_stability/api_stability_core.py#L79-L90) 中，把每个参数的 `annotation`/`default`/`status` 固化下来。已提交的参考快照放在 `tests/unittest/api_stability/references_committed/`（如 `llm.yaml`、`sampling_params.yaml`）。

**为什么这层重要。** [`CONTRIBUTING.md:122-L141`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/CONTRIBUTING.md#L122-L141) 旁注：受保护 API（目前主要是 LLM API 的核心组件）的破坏性改动会让该测试 fail，并要求找 code owner review；改动参考快照时需在 PR 标题用 `api-compatible` 或 `api-breaking`（后者还要加 `BREAKING`）。[`AGENTS.md:121-L122`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/AGENTS.md#L121-L122) 也提醒：改 LLM API 签名会 fail 这组测试，需 code owner 审查。

#### 4.1.4 代码实践：阅读一个单元测试并理解它属于哪一层

1. **实践目标**：亲手定位一个单元测试，判断它「是否需要 GPU/模型」「会被哪条流水线跑」。
2. **操作步骤**：
   - 打开 `tests/unittest/_torch/sampler/test_beam_search.py`，阅读其中一个 `test_*` 函数的断言。
   - 用 `grep` 在 `tests/integration/test_lists/test-db/` 下搜该文件名，看它在哪些硬件 YAML 里被登记、`stage` 是什么：
     ```bash
     grep -rn "test_beam_search.py" tests/integration/test_lists/test-db/
     ```
3. **需要观察的现象**：它既是一个**单元测试**（位于 `tests/unittest/`，merge-request 流水线全量跑），又同时被登记进 `l0_a10.yml`、`l0_a30.yml` 等（即也会在对应硬件的 pre_merge stage 再跑一遍）。这正说明「单元/集成」的边界不是绝对的——同一个测试可以在两层都被纳入。
4. **预期结果**：在多个 `l0_*.yml` 里命中，且所在条件块的 `stage` 多为 `pre_merge`。
5. 因本环境无法访问这些文件的实际运行，上述 grep 命令的精确命中数**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么单元测试和 API 稳定性测试都放在 `tests/unittest/`，却不混进集成测试目录？
> **答案**：它们都不需要 GPU + 真实模型权重，成本低、可在 merge-request 流水线快速跑；集成测试成本极高，必须按硬件 YAML 单独登记并触发。

**练习 2**：你在 `tests/unittest/api_stability/` 改了 `LLM` 类的某个公开参数默认值，CI 会怎样？
> **答案**：API 稳定性测试会把「运行时 inspect 到的签名」与 `references_committed/llm.yaml` 比对，发现不一致而 fail。你需要更新该参考快照，并在 PR 标题用 `api-compatible`（兼容）或 `api-breaking`+`BREAKING`（破坏性），同时找 code owner review。

---

### 4.2 CI 触发与 stage 映射

#### 4.2.1 概念说明

PR **不会自动跑测试**——必须在 PR 里评论 `/bot run` 才触发。CI 把工作切成一个个 **stage**（阶段），每个 stage 绑定「一种硬件 + 一份 YAML 清单 + 一个分片」。stage 名是有语义的，例如 `A10-PyTorch-1` 表示「A10 GPU、PyTorch 后端、第 1 分片」。

问题来了：仓库里有几千个测试、几十份 YAML，**「我改的这个测试，到底归哪个 stage 管？」** 手工翻 YAML + Groovy 太累，于是有了官方反查工具 [`scripts/test_to_stage_mapping.py`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/scripts/test_to_stage_mapping.py)。它做两件事的双向映射：

- 给**测试名** → 列出跑它的 stage；
- 给 **stage 名** → 列出它跑的所有测试。

#### 4.2.2 核心流程

反查工具的数据来自两处，在启动时一次性加载：

```text
jenkins/L0_Test.groovy  ──▶ 正则提取 (stage 名 → YAML 文件 + 分片) 映射
tests/.../test-db/*.yml ──▶ 解析 (测试名 → (yml, stage, backend)) 映射
                                 │
                                 ▼
                       拼接：测试名 → stage 名
```

正查（test→stage）的核心是：先在 YAML 里找到该测试出现过的 `(yml, stage_type, backend)`，再用「yml 反查出的 stage 列表」按 `pre_merge/post_merge` 与 `backend` 两道闸过滤。其中：

- `stage_type == post_merge` 要求 stage 名含 `Post-Merge`；`pre_merge` 要求不含。
- `backend`（如 `pytorch`）要求 stage 名里出现对应关键词（动态推导，如 `PYTORCH`/`TORCH`）。

#### 4.2.3 源码精读

**解析 Groovy 的正则。** 工具用一个正则从 `L0_Test.groovy` 抠出 `"stage名": ["平台", "yaml文件", 分片参数...]` 这类行——[scripts/test_to_stage_mapping.py:62-L64](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/scripts/test_to_stage_mapping.py#L62-L64) 定义 `_STAGE_RE`，它捕获两个组：`stage` 名与 `yml` 文件名（随后补 `.yml`）。对应 Groovy 里的真实写法可对照 [`jenkins/L0_Test.groovy`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/jenkins/L0_Test.groovy)，例如 `"A10-PyTorch-1": ["a10", "l0_a10", 1, 3]`（平台 `a10`、清单 `l0_a10`、第 1/3 分片）。

**构造 `StageQuery` 时建好两张表。** [scripts/test_to_stage_mapping.py:75-L98](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/scripts/test_to_stage_mapping.py#L75-L98) 中的 `_parse_stage_mapping` 产出 `stage_to_yaml` 与 `yaml_to_stages` 双向索引；[scripts/test_to_stage_mapping.py:100-L128](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/scripts/test_to_stage_mapping.py#L100-L128) 的 `_parse_tests` 遍历 `test-db/*.yml`，给每个测试记下它出现过的 `(yml, stage, backend)` 三元组。

**正查的两道过滤闸。** [scripts/test_to_stage_mapping.py:168-L190](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/scripts/test_to_stage_mapping.py#L168-L190) 的 `tests_to_stages` 就是上面流程图的落地：先按 `Post-Merge` 字样筛 stage 类型，再按 `backend` 关键词筛后端。

**主入口。** [scripts/test_to_stage_mapping.py:224-L266](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/scripts/test_to_stage_mapping.py#L224-L266) 的 `main` 用互斥参数组 `--tests / --test-list / --stages` 三选一，`--tests` 支持多个模式、按子串匹配（`search_tests` 在 [scripts/test_to_stage_mapping.py:159-L166](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/scripts/test_to_stage_mapping.py#L159-L166) 把空格分隔的多个子串做 AND 匹配）。

**触发命令。** 反查到 stage 名后，按 [`AGENTS.md:168-L170`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/AGENTS.md#L168-L170) 与 [docs/source/developer-guide/ci-overview.md:108-L147](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/developer-guide/ci-overview.md#L108-L147) 的说明在 PR 评论：

- `/bot run` —— 触发标准 pre-merge 流水线；
- `/bot run --stage-list "stage-A,stage-B"` —— 只跑列出的 stage（省硬件）；
- `/bot run --extra-stage "stage-A"` —— 在默认集合之上追加；
- `/bot run --disable-fail-fast` —— 即使前面 stage 失败也跑完（**慎用**，浪费 GPU，CI 会自动复用成功的 stage）；
- 全量 `/bot run --post-merge` / `--stage-list "*Post-Merge*"` 等通配符需 `ci: post-merge approved` 标签（资源治理）。

**豁免机制。** 已知失败用例不删测试，而是登记到 `tests/integration/test_lists/waives.txt`，每行一条「完全限定名 + `SKIP (bug链接)`」，CI 用 `--waives-file` 自动跳过——见 [tests/integration/test_lists/waives.txt:1-L5](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tests/integration/test_lists/waives.txt#L1-L5)，可用 `full:GPU_TYPE/` 前缀限定硬件族。

#### 4.2.4 代码实践：用映射脚本反查 stage

1. **实践目标**：把一个测试名反查到它对应的 Jenkins stage，并组织出对应的 `/bot run` 评论。
2. **操作步骤**（仓库根目录下）：
   ```bash
   # 正查：测试名 → stage
   python scripts/test_to_stage_mapping.py --tests "unittest/_torch/sampler/test_beam_search.py"
   # 子串匹配也行
   python scripts/test_to_stage_mapping.py --tests test_beam_search
   # 反查：stage → 它跑哪些测试
   python scripts/test_to_stage_mapping.py --stages "A10-PyTorch-1"
   ```
3. **需要观察的现象**：正查命令应打印一串 stage 名（多为 `A10-PyTorch-*`、`A100X-*`、`*-PyTorch-*` 等，因该测试在多个硬件 YAML 里登记为 `pre_merge`+`pytorch`）；反查命令应打印 `l0_a10.yml` 中 `pre_merge`/`pytorch` 段下的测试列表。
4. **预期结果**：正查输出的 stage 名**都不含** `Post-Merge`（因为该测试在 YAML 里标的是 `pre_merge`），且都含 `PyTorch` 关键词（因为 `backend=pytorch`）。
5. 因本环境无法执行该脚本，**精确输出列表待本地验证**；但「不含 Post-Merge、含 PyTorch」这一规律可由源码的两道过滤闸直接推断。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tests_to_stages` 里要对 `post_merge` 的 stage 额外要求名字含 `Post-Merge`？
> **答案**：同一份 YAML 文件（如 `l0_a100`）既可能被 pre-merge stage 引用，也可能被 post-merge stage 引用。仅靠「yml 匹配」无法区分二者，必须再按 stage 名里的 `Post-Merge` 字样与 YAML 条目的 `stage` 字段对齐，才能保证 pre-merge 用例只在 pre-merge stage 跑、反之亦然。

**练习 2**：`/bot run --disable-fail-fast` 何时才该用？
> **答案**：仅当你确实需要看到**所有** stage 的结果（而非失败即停）时。CI 会自动复用未变更 commit 的成功 stage、后续 `/bot run` 只重跑失败 stage，所以日常应让 fail-fast 生效以省 GPU；滥用 `--disable-fail-fast` 会拖垮共享硬件队列。

---

### 4.3 贡献规范：DCO、PR 标题、版权头、pre-commit

#### 4.3.1 概念说明

TensorRT-LLM 是 NVIDIA 维护的开源项目，合入 `main` 的门槛不只是「测试通过」，还包括一套**合规与协作规范**，目的是让成百上千的贡献者高效协作、让 release 可追溯：

- **DCO 签名**：每个 commit 必须有 `Signed-off-by`，法律层面声明代码来源合规。
- **PR 标题格式**：`[追踪号][类型] 描述`，便于追踪「哪条改动进了哪个 release」。
- **NVIDIA 版权头**：每个源文件顶部必须有版权声明；修改已有文件要更新年份。
- **pre-commit**：commit 前自动跑格式化 + lint，保证风格统一。
- **golden manifest**：动了用户配置（LLM args）要重生成并提交一份「黄金清单」，供遥测/隐私 review。

#### 4.3.2 核心流程

贡献一条改动的端到端流程：

```text
1. 提 Issue（enhancement/bugfix）并被 NVIDIA 工程师批准
2. fork 上游 → clone fork → 建分支改代码
3. 改动若触及 LLM args/嵌套配置：
   - 跑 python3 scripts/generate_llm_args_golden_manifest.py
   - 提交更新后的 tensorrt_llm/usage/llm_args_golden_manifest.json
4. 给新增/修改的源文件维护 NVIDIA 版权头（新文件加整段、改过的更新年份）
5. git commit -s ...        # DCO 签名（用 -s，别手写 sign-off）
   └─ pre-commit 自动跑；若改了文件，重新 git add 再 commit
6. 推到 fork（origin），向上游 main 提 PR，标题用 [追踪号][类型] 描述
7. PR 描述写背景/摘要/影响；评论 /bot run 触发 CI
8. 通过 review + CI → 合入
```

#### 4.3.3 源码精读

**硬规则速查表。** [`AGENTS.md:11-L20`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/AGENTS.md#L11-L20) 把最关键的几条列在一起：所有新文件加 NVIDIA 版权头、改过的文件更新年份；`git commit -s` 做 DCO、且**不要在 sign-off 行提到 AI 工具**；动 LLM args 要重跑 golden manifest 脚本；PR 标题格式 `[JIRA/NVBUG/None][type] description`；集成测试要设 `LLM_MODELS_ROOT`。

**DCO 的法律含义。** [`CONTRIBUTING.md:144-L157`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/CONTRIBUTING.md#L144-L157) 说明：`git commit -s -m "..."` 会追加 `Signed-off-by: Your Name <your@email.com>`，表示你接受 DCO 条款；**未签名的 commit 一律不接受**。注意 [AGENTS.md:13](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/AGENTS.md#L13) 的强调——「Always rely on `git` to do the sign off instead of directly adding sign off in commit message」，即让 `-s` 自动生成，不要手敲。

**PR 标题与策略。** [`CONTRIBUTING.md:87-L104`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/CONTRIBUTING.md#L87-L104) 给出 Conventional Commits 风格示例：`feat: ...`、`BREAKING CHANGE: ...`、`chore: ...`、`[TRTLLM-5516] perf: ...`（NVIDIA 内部带 JIRA/NVBUG 号）。破坏性 API 改动要在标题加 `BREAKING`。同时 [CONTRIBUTING.md:60-L62](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/CONTRIBUTING.md#L60-L62) 要求每个 PR 尽量只解决「一个关注点」，别在一个 PR 里塞无关改动。

**pre-commit 的「双组」lint。** [`CODING_GUIDELINES.md:777-L834`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/CODING_GUIDELINES.md#L777-L834) 是最容易踩坑的地方：Python 文件被分成两组——
- **Group A（现代，约 550 文件）**：用 `ruff format`（100 字符宽）+ 全套 ruff 规则。
- **Group B（legacy，约 1350 文件，列在 `legacy-files.txt`）**：用 `yapf`（80 字符）+ `isort` + `autoflake` + 补充 ruff 规则，且**基线 gated**——已有违规容忍，但你的改动**新增**违规会被拦。

[`CONTRIBUTING.md:13-L58`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/CONTRIBUTING.md#L13-L58) 展示一次 commit 里 pre-commit 跑的 hook 清单（isort/yapf/ruff/ruff-format/clang-format/cmake-format/codespell/mdformat…），并强调：**若 hook 改了文件，必须重新 `git add` 再 commit**。

**NVIDIA 版权头。** [`CODING_GUIDELINES.md:908-L927`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/CODING_GUIDELINES.md#L908-L927) 给出标准版权块（`Copyright (c) <年份>, NVIDIA CORPORATION. All rights reserved.` + Apache 2.0 许可文本），要求加在所有 `.cpp/.h/.cu/.py` 等源文件顶部，且年份取「最近一次有意义修改」的年份。

#### 4.3.4 代码实践：写一份「提交 PR 前的自检清单」

1. **实践目标**：把本节规范固化成一份可勾选的 checklist，今后每次提 PR 前过一遍。
2. **操作步骤**：在本地建一个 `pr-checklist.md`（注意：这是你自己的备忘文件，**不要**放进仓库的 `TensorRT-LLM-tutorial/` 之外，也不要提交到上游），写入以下内容（下文给出参考答案）。
3. **需要观察的现象**：用它自检一个你（假想）正在做的 commit——比如你新增了一个 `tensorrt_llm/_torch/modules/foo.py` 并给 `TorchLlmArgs` 加了个字段。
4. **预期结果**：能逐条指出「这一项该做什么」。
5. 该自检清单的完整内容见下文「参考答案」，命令是否实际通过**待本地验证**。

**参考答案（提交 PR 前的自检清单）**：

- [ ] **Issue**：是否已提 Issue 并被 NVIDIA 工程师批准？（所有 enhancement/bugfix 都要先有 Issue）
- [ ] **DCO**：每个 commit 都用 `git commit -s` 签名？sign-off 行**没有**提到 AI 工具？
- [ ] **PR 标题**：符合 `[JIRA/NVBUG/None][type] 描述`？若有 API 破坏性改动，标题含 `BREAKING`？
- [ ] **单一关注点**：本 PR 只解决一件事，没有夹带无关改动、没有注释掉的死代码？
- [ ] **版权头**：新文件加了完整 NVIDIA 版权块？改过的文件年份更新到今年？
- [ ] **pre-commit**：本地 `pre-commit run --all-files`（或直接 commit）通过？若 hook 改了文件，已重新 `git add` 再 commit？legacy 文件改动**没有新增** ruff 违规？
- [ ] **LLM args**：若改了 `llm_args.py`/嵌套配置，已跑 `python3 scripts/generate_llm_args_golden_manifest.py` 并提交了 `llm_args_golden_manifest.json`？新字段是否需遥测/隐私 CODEOWNER 审批？
- [ ] **API 稳定性**：若改了 `LLM`/`SamplingParams` 等公开签名，`tests/unittest/api_stability` 是否通过？参考快照是否已更新并标注 `api-compatible`/`api-breaking`？
- [ ] **测试**：相关单元测试本地 `pytest` 通过？涉及集成测试时已设 `LLM_MODELS_ROOT` 并能用 `test_to_stage_mapping.py` 反查出要跑的 stage？
- [ ] **CI**：PR 描述写了背景/摘要/影响/关联链接？评论了精准的 `/bot run --stage-list "..."` 而非滥用全量或 `--disable-fail-fast`？

#### 4.3.5 小练习与答案

**练习 1**：你 `git commit -m "..."` 忘了 `-s`，怎么补救？
> **答案**：对**最新**一个 commit 用 `git commit --amend -s --no-edit` 补签名；若是多个历史 commit，用 `git rebase --signoff <base>..HEAD`（注意本仓库不支持交互式 `-i`，且 rebase 后需强推 fork 分支）。

**练习 2**：你修改了一个 Group B（legacy）文件，pre-commit 报 ruff 违规被拦，但你觉得「这文件本来就这么写」。该怎么判断？
> **答案**：legacy hook 是**基线 gated**——只拦「你的改动**新增**的违规」，已有违规记在 `ruff-legacy-baseline.json` 快照里被容忍。所以若被拦，说明你确实新引入了违规，应按提示修复；若是清理了既有违规，可跑 `python scripts/legacy_utils.py lint-update-violations` 收紧基线并连同改动一起提交。

**练习 3**：给 `TorchLlmArgs` 加了一个新字段 `foo`，除了写代码还要做什么？
> **答案**：跑 `python3 scripts/generate_llm_args_golden_manifest.py` 并提交更新后的 `tensorrt_llm/usage/llm_args_golden_manifest.json`；若新字段涉及遥测/隐私，还需相应 CODEOWNER 审批（见 [AGENTS.md:16-L17](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/AGENTS.md#L16-L17)）。

## 5. 综合实践

把本讲三块串起来，模拟一次完整的「改一行代码到触发 CI」：

**背景**：假设你发现 `unittest/_torch/sampler/test_beam_search.py` 在某硬件上偶发失败，你想先搞清楚「这条测试到底被哪些 CI stage 覆盖」，再决定要不要提 PR。

**任务**：

1. **分层判定**：用 `grep` 确认它是单元测试（位于 `tests/unittest/`）还是同时被集成 YAML 登记，得出「它会被 merge-request 流水线全量跑、且被若干 pre_merge stage 再跑」的结论。
2. **stage 反查**：跑
   ```bash
   python scripts/test_to_stage_mapping.py --tests test_beam_search
   ```
   记录输出的 stage 列表，挑选一个最贴近你目标硬件的（如 `A10-PyTorch-1`）。
3. **触发 CI**：写出在该 PR 上只重跑这个 stage 的评论：
   ```text
   /bot run --stage-list "A10-PyTorch-1"
   ```
4. **豁免备案**：若该失败短期修不掉，写出一行加入 `waives.txt` 的格式（含 `SKIP (bug链接)`），并说明这比「删测试」更可取的原因。
5. **贡献合规**：假设你随后写了修复 commit，写出完整的提交命令（含 `-s`）与一个合规的 PR 标题（自选追踪号与类型）。

**交付物**：一份简短报告，包含上述 5 步的命令、输出要点与结论。stage 反查的精确输出**待本地验证**，但「输出的 stage 名均含 `PyTorch`、不含 `Post-Merge`」可由源码逻辑预先断言。

## 6. 本讲小结

- TensorRT-LLM 测试分三层：**单元测试**（`tests/unittest/`，merge-request 全量跑、无需 GPU）、**集成测试**（`tests/integration/defs/`，要 GPU+模型、按硬件 YAML 登记）、**API 稳定性测试**（`tests/unittest/api_stability/`，比对公开签名快照守护接口契约）。
- CI 由 PR 评论 `/bot run` 触发，分 **merge-request**（pre_merge + 单元）与 **post-merge**（post_merge，跨硬件）两条流水线；`/bot run --stage-list` 精准触发、`--disable-fail-fast` 慎用、通配/post-merge 需审批标签。
- `scripts/test_to_stage_mapping.py` 用「正则解析 Groovy + 解析 YAML 清单」建立测试名 ↔ stage 的双向映射，正查经「yml + Post-Merge 字样 + backend 关键词」三道闸过滤。
- 已知失败用 `waives.txt` 豁免而非删除，每行带 bug 链接，可按硬件族限定。
- 贡献合规五件套：`git commit -s` 的 DCO 签名（勿提 AI）、`[追踪号][类型]` PR 标题、NVIDIA 版权头（新文件加整段/改过更新年份）、pre-commit 双组 lint（legacy 基线 gated）、改 LLM args 重跑 golden manifest。
- 改受保护 API 会 fail API 稳定性测试，需更新参考快照并标注 `api-compatible`/`api-breaking`，找 code owner review。

## 7. 下一步学习建议

- **动手提一个真 PR**：从修一个 `good first issue` 或补一条单元测试开始，完整走一遍本讲的 checklist，把「读」变成「写」。
- **深读 API 变更指南**：[`docs/source/developer-guide/api-change.md`](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/developer-guide/api-change.md) 讲清了 `api-compatible`/`api-breaking` 的判定与参考快照更新细则，是 4.1 节的自然延伸。
- **通读 `CODING_GUIDELINES.md` 全文**：本讲只覆盖了它的 pre-commit 与版权头部分；C++（Allman 风格、命名、east-const）与 Python（类型注解、Pydantic 规范）章节是高质量代码的标尺。
- **回到手册其他单元对照**：你现在已具备「贡献者视角」，可重读 [u4-l1 TorchLlmArgs 与配置层级](u4-l1-llm-args-hierarchy.md)，理解为什么「动 LLM args 必须跑 golden manifest」是一道硬约束。
