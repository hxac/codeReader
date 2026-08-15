# u7-l2 CI 流水线与版本发布机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚一个 PR 从提交到合入，仓库的 CI 门禁到底跑了哪些检查、由哪些入口参数触发。
2. 理解「changed files 变更文件清单」如何被 `tests/test_config.yaml` 和 `cmake/scripts/parse_changed_files.py` 解析成「要跑哪些算子的 UT / example / 出哪些包」，即按需裁剪机制。
3. 区分 `classify_rule.yaml`（组件划分与交付范围）与 `tests/test_config.yaml`（CI 测试裁剪）两份配置的分工。
4. 掌握 `--pkg-type=run/rpm/deb/all` 四种包型的差异、各自适用的场景与版本约束。
5. 读懂 `version.cmake` 中的版本号与 CANN 包依赖声明，理解「版本配套」在发布物里是如何落地的。

## 2. 前置知识

- **CI（Continuous Integration，持续集成）**：代码合入前的自动化检查流水线。本仓库托管在 GitCode 上，贡献者在 PR 下评论 `compile` 指令即可触发门禁（见第 4.1 节）。
- **changed files（变更文件清单）**：一个 PR 中所有被修改/新增/删除文件的相对路径列表，一行一个路径。CI 拿到它之后才能做「按需裁剪」——只测被改到的算子，而不是全仓几百个算子都跑一遍。
- **门禁（gate）**：CI 检查不通过，PR 就不能合入。本仓库门禁包含：代码编译、静态检查、UT 测试、冒烟测试四项。
- **run / rpm / deb 包**：三种安装包格式。`.run` 是华为 CANN 生态自带的 makeself 自解压脚本包（`bash xxx.run` 安装）；`.rpm` 和 `.deb` 分别是 RedHat 系与 Debian 系 Linux 的标准包管理器格式（`rpm -i` / `dpkg -i` 安装）。
- **版本配套**：u1-l3 已讲过源码标签必须与 CANN 版本配套；本讲会看到这套约束在 `version.cmake` 里被声明为 `>=8.5` 的依赖关系，并写进安装包里做安装期校验。
- **SoC 矩阵**：仓库支持 ascend310p / ascend910b / ascend910_93 / ascend950 等多代芯片。CI 不可能对每个算子都跑全矩阵，而是按「变更文件属于哪个算子 → 该算子支持哪些 SoC」来裁剪。

前置讲义依赖：u7-l1 讲过四类 UT 的写法与 `test_config.yaml` 在 CI 看护中的作用，本讲把视角拉高到整条流水线；u1-l4 讲过 build.sh 的参数体系，本讲深入其中 PR 相关的三个分支。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `build.sh` | 唯一构建入口。包含 `--PR_UT`、`--PR_PKG`、`-f/--changed_list` 三个 CI 分支，以及 `--pkg-type` 校验与 `build_package` 出包循环 |
| `tests/test_config.yaml` | CI 测试裁剪规则：目录结点 → 看护源码 → 触发的算子验证（UT / example） |
| `tests/test_soc_config.yaml` | CI 的 SoC 裁剪规则：变更文件 → 需要跑 UT 的芯片型号列表 |
| `cmake/scripts/parse_changed_files.py` | 解析器：读入上述两份 yaml + 变更文件清单，输出「要测的算子集合」 |
| `scripts/ci/parse_changed_ops.py` | experimental 场景下从变更文件提取算子名的另一套解析器 |
| `scripts/ci/ascend910b/`、`scripts/ci/ascend950/` | 按 SoC 分组的算子清单（`ops_transformer_operator_list.yaml`），用于把真机测试拆组并行 |
| `classify_rule.yaml` | 组件划分信息：按「责任人@仓库」声明源码交付风格与非交付目录，与 CI 测试裁剪互补 |
| `version.cmake` | 版本号（9.1.0）与 CANN 构建/运行依赖（>=8.5）声明 |
| `CMakeLists.txt` | `PACKAGE_TYPE` 缓存变量校验、`version.cmake` 引入、不支持芯片的空包兜底 |
| `cmake/package.cmake` | `pack_built_in` / `pack_custom` 两个出包函数，最终经 `set_cann_cpack_config` 调 CPack |
| `CONTRIBUTING.md` | 社区协作流程中对 CI 门禁的官方描述 |

## 4. 核心概念与源码讲解

本讲按「CI 流水线」与「包管理」两大最小模块展开，其中流水线再细分为门禁入口、变更解析、按需 UT、按需出包四步。

### 4.1 CI 门禁全景：PR 场景的入口变量

#### 4.1.1 概念说明

一个 PR 推到 GitCode 后，门禁要回答的问题是：**这次改动波及哪些算子？对这些算子需要做哪种验证？** 仓库的答案是：CI 系统先把 PR 的变更文件清单写到一个临时文件里，然后调用 `build.sh` 时把这个文件路径传进来。`build.sh` 里有一组专门的「PR 场景状态变量」，它们的出现与否就是判断「这是不是 CI 运行」的标识。

#### 4.1.2 核心流程

```text
开发者提交 PR
    │  评论 compile 指令
    ▼
GitCode CI 拉取代码，导出变更文件清单（一行一个相对路径）
    ▼
调用 build.sh 的三条 CI 通道（互斥使用）：
    ├─ bash build.sh --PR_UT  <changed_files>     → 按需跑单元测试
    ├─ bash build.sh --PR_PKG  <changed_files>     → 按需编译安装包（自定义包）
    └─ bash build.sh --run_example -f <changed_files> → 冒烟：按需编译并运行 example
    ▼
门禁四项：代码编译 / 静态检查 / UT 测试 / 冒烟测试
    ▼
Committer 检视 → /lgtm → Maintainer /approve → 合入
```

#### 4.1.3 源码精读

CONTRIBUTING.md 中对门禁的官方定义：

> 通过评论 `compile` 指令触发开源仓门禁，并依据 CI 检测结果进行修改。目前 CI 门禁包含以下检查项：代码编译、静态检查、UT 测试、冒烟测试。

见 [CONTRIBUTING.md:103-112](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L103-L112)，这段还给出了门禁通过后的协作链路：@ Committer → `/lgtm` → `/approve` 合入。

`build.sh` 顶部的 PR 场景状态变量：

```bash
PR_CHANGED_FILES=""  # PR场景, 修改文件清单, 可用于标识是否PR场景
UT_SOC_ARRAY=()
UT_TEST_CNT=0
PR_UT_FLAG=FALSE
CI_MODE=FALSE
```

见 [build.sh:75-79](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L75-L79)。注意注释里的关键句：「**可用于标识是否PR场景**」——`PR_CHANGED_FILES` 是否为空，就是脚本内部区分「开发者本地构建」与「CI 构建」的开关，后文多个分支都会先判空。

#### 4.1.4 代码实践

1. **实践目标**：不依赖任何 CI 系统，手工模拟一次「CI 视角」的变更解析。
2. **操作步骤**：
   - 在仓库根目录新建临时文件 `changed.txt`，写入两行（模拟改了 FA 算子的 host 源码）：
     ```text
     attention/flash_attention_score/op_host/flash_attention_score_tiling.cpp
     attention/flash_attention_score/README.md
     ```
   - 执行解析器（这正是 `--PR_UT` 分支内部跑的命令）：
     ```bash
     python3 cmake/scripts/parse_changed_files.py \
       -c tests/test_config.yaml -f changed.txt get_related_ut
     ```
3. **需要观察的现象**：README.md 那一行不会触发任何结果；输出只有 `flash_attention_score;`。
4. **预期结果**：输出为 `flash_attention_score;`（分号分隔的算子清单）。原因见 4.2 节的 `exclude` 与 `file_filter`。完成后删除 `changed.txt`。本实践纯 host 侧 Python，无需 NPU（`pip3 install pyyaml` 即可，仓库 `requirements.txt` 已含该依赖）。

#### 4.1.5 小练习与答案

**练习 1**：如果 CI 想新增一项「代码覆盖率」检查，应该改 `build.sh` 还是另起脚本？
**答案**：优先另起脚本或扩展 `--PR_UT` 分支。`build.sh` 是参数翻译器（u1-l4 的结论），覆盖率开关 `-cov` 已在 test 帮助中提供（[build.sh:145](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L145)），CI 侧组合调用即可，不必新造入口。

**练习 2**：`PR_CHANGED_FILES` 为什么要转成绝对路径（`parse_changed_files` 里有 `if [[ "$PR_CHANGED_FILES" != /* ]]` 判断）？
**答案**：CI 传入的可能是相对路径，而后续 `python3` 子进程的工作目录会随 `cd ${BUILD_DIR}`（[build.sh:2208](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2208)）变化，相对路径会失效；提前补全为 `$PWD/...` 前缀保证解析器随时能读到。见 [build.sh:1204-1217](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1204-L1217)。

### 4.2 changed files 解析引擎：test_config.yaml + parse_changed_files.py

#### 4.2.1 概念说明

这是整条流水线的「大脑」。`tests/test_config.yaml` 用一棵目录树声明「哪个目录被改了要触发哪些算子的验证」，`cmake/scripts/parse_changed_files.py` 负责拿变更文件去这棵树上做路径匹配。与之配套的 `tests/test_soc_config.yaml` 再回答「这些算子要在哪些芯片上测」。

要特别注意它和 `classify_rule.yaml` 的分工（两份文件开头互相引用，见 [classify_rule.yaml:10-11](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/classify_rule.yaml#L10-L11)）：

| 配置 | 回答的问题 | 维度 |
| --- | --- | --- |
| `classify_rule.yaml` | 这批源码归谁负责、以什么风格交付、哪些目录不交付 | 人 / 组件 / 发布范围 |
| `tests/test_config.yaml` | 这批源码被改后要触发哪些 UT / example / 包 | 算子 / 测试 |

#### 4.2.2 核心流程

`test_config.yaml` 的最小配置单位是「结点」，文件头注释给出了完整 schema（[tests/test_config.yaml:11-46](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L11-L46)）：

```yaml
op_name:              # 结点名，一般为算子名
  module: True        # 固定写法，标识此处为一个结点（递归解析的终止标志）
  src:                # 看护的源码列表：这里的文件被改 → 触发 options 里的算子验证
  exclude:            # 排除列表：这些文件被改不触发（如 docs/、README.md）
  ut_cov_exclude:     # 覆盖率统计时排除的路径
  test:               # 校验开关：examples: True/False, ut: True/False
  options:            # 被触发时要一起验证的算子清单（不递归触发）
```

解析流程：

```text
parse_changed_files.py
  ├─ parse_classify_file(test_config.yaml) → 递归下钻，遇 "module" 键停止，收集 Module 列表
  ├─ parse_changed_file(changed.txt)       → 逐行读路径；file_filter 滤掉 .md/.json/.ini 与 docs/
  └─ 子命令求值（对每个变更文件 × 每个 Module 做路径前缀匹配）
       get_related_ut             → 全量相关 UT 算子集
       get_related_ut_mc2         → 只统计路径里含 mc2 的
       get_related_ut_exclude_mc2 → 排除路径里含 mc2 的
       get_related_examples       → 需要跑 example 的算子集
```

#### 4.2.3 源码精读

一个真实结点——flash_attention_score 的看护声明：

```yaml
flash_attention_score:
  module: True
  src:
    - attention/flash_attention_score
  exclude:
    - attention/flash_attention_score/docs
    - attention/flash_attention_score/README.md
  ut_cov_exclude:
    - attention/flash_attention_score
  test:
    ut: True
  options:
    - flash_attention_score
```

见 [tests/test_config.yaml:108-121](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L108-L121)：src 看护整个算子目录，exclude 把 docs 和 README 排除（所以 4.1.4 实践里改 README 不触发），`test.ut: True` 允许触发 UT。也可以只写 `examples: False` 关掉某类验证——examples/mc2/all_gather_add 结点就同时关了 examples 和 ut（[tests/test_config.yaml:60-77](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L60-L77)）。

路径匹配的核心逻辑（Module.get_test_options）：

```python
def is_excluded(e_f: Path):
    for e in self.src_exclude_files:
        try:
            e_f.relative_to(e)   # 变更文件能相对到排除目录 → 命中排除
            return True
        except ValueError:
            continue
    return False
...
for s in self.src_files:
    if is_excluded(e_f=f):
        continue
    try:
        if f.relative_to(s):          # 变更文件落在看护目录下
            related_options.extend(self.options)  # 触发该结点全部 options
    except ValueError:
        continue
```

见 [parse_changed_files.py:66-88](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/scripts/parse_changed_files.py#L66-L88)：先排除后匹配，`Path.relative_to` 实现的是「目录前缀包含」语义；同一文件命中多个结点时 options 会合并去重（这正是 options 可以声明「连带验证」的原因，例如某算子的 example 需要友邻算子一起跑）。

文件级过滤，双保险挡住非源码变更：

```python
exclude_extensions = [".md", ".json", ".ini"]
exclude_keywords = ["docs/"]
```

见 [parse_changed_files.py:191-204](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/scripts/parse_changed_files.py#L191-L204)。所以「只改了文档」的 PR 天然不会触发任何 UT——这是低噪声门禁的第一道闸。

四个子命令的注册与分发见 [parse_changed_files.py:166-188](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/scripts/parse_changed_files.py#L166-L188)；`get_related_ut_mc2` 与 `get_related_ut_exclude_mc2` 的差别只在 `if "mc2" not in p.parts: continue` 这一行的正反（[parse_changed_files.py:326-361](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/scripts/parse_changed_files.py#L326-L361)）——为什么要把 mc2 单独拆出来，见 4.3 节。结果字符串的拼装与 `all` 短路逻辑见 [parse_changed_files.py:364-374](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/scripts/parse_changed_files.py#L364-L374)：任一结点 options 含 `all` 即输出全量。

SoC 裁剪用的是同构的另一份规则。`tests/test_soc_config.yaml` 的 all_soc 结点：

```yaml
all_soc:
  module: True
  src:
    - mc2/common
    - mc2/3rd
  options:
    - ascend310p
    - ascend910b
    - ascend950
```

见 [tests/test_soc_config.yaml:14-22](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_soc_config.yaml#L14-L22)：公共代码（mc2/common、mc2/3rd）被改动时影响面无法界定，只能在全部三 SoC 上测；而只支持特定芯片的算子则声明更窄的 options（如 ascend310p 结点，[tests/test_soc_config.yaml:24-30](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_soc_config.yaml#L24-L30)）。

`classify_rule.yaml` 侧看一个组件条目的样子（节选）：

```yaml
AAG_David@ops-transformer:
    src:
        release:
            huawei_style:
                - ops/ops-transformer/attention/flash_attention_score_grad/op_host/arch35
                - ops/ops-transformer/attention/flash_attention_score_grad/op_kernel/arch35
            opensource_style: null
            kernel_style: null
        unrelease:
            test_code: null
            non_delivery: null
            open_source: null
```

见 [classify_rule.yaml:48-59](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/classify_rule.yaml#L48-L59)。顶层键是「责任人@仓库」，release 下按交付风格（huawei_style / opensource_style / kernel_style）列交付文件，unrelease 下声明不随包发布的目录（最常见的是 tests / examples / docs，如 [classify_rule.yaml:121-134](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/classify_rule.yaml#L121-L134)），llt.ut_filter 声明该组件走低层测试时按路径过滤用例（[classify_rule.yaml:135-145](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/classify_rule.yaml#L135-L145)）。这份文件服务于内部组件化发布流程，与开源仓 CI 的运行时裁剪互补。

#### 4.2.4 代码实践

1. **实践目标**：亲手体验「exclude 连带排除」与「options 连带触发」两个语义。
2. **操作步骤**：
   - 准备 `changed2.txt`，内容两行：
     ```text
     attention/flash_attention_score_grad/op_host/arch35/fia_tiling.h
     examples/mc2/all_gather_add/run.sh
     ```
   - 分别运行两个子命令并对比输出：
     ```bash
     python3 cmake/scripts/parse_changed_files.py -c tests/test_config.yaml -f changed2.txt get_related_ut
     python3 cmake/scripts/parse_changed_files.py -c tests/test_config.yaml -f changed2.txt get_related_examples
     ```
   - 再把 `mc2/all_gather_matmul/op_host/aclnn_all_gather_matmul.cpp` 加进清单，重跑 `get_related_ut_mc2` 与 `get_related_ut_exclude_mc2`。
3. **需要观察的现象**：all_gather_add 结点因 `test: examples/ut 均为 False` 两个命令都不输出它；mc2 算子只出现在 mc2 子命令的结果里。
4. **预期结果**：第三步得到互斥的两个集合——`get_related_ut_mc2` 输出 `all_gather_matmul;`，`get_related_ut_exclude_mc2` 输出 `flash_attention_score_grad;`。这就是 build.sh 把 UT 拆两批跑的输入。待本地验证（取决于 yaml 当前内容，若结点配置有更新以实际输出为准）。

#### 4.2.5 小练习与答案

**练习 1**：新贡献一个算子（u6-l1 的 my_sum），需要在哪里登记才能被 CI 看护？
**答案**：在 `tests/test_config.yaml` 对应域下新增结点：`src` 填算子根目录、`exclude` 排除 docs 与 README、`options` 填算子名。漏登记的后果是：改坏了也不会触发 UT——CI 静默放行。
**练习 2**：为什么 `file_filter` 要排除 `.ini`？
**答案**：op_host/config 下的 tiling ini（u2-l1 讲过）是编译期配置，变更它需要重编译验证，但不属于 UT 用例覆盖的源码对象；CI 对它的验证走「编译」门禁而非 UT。
**练习 3**：`options` 里写别的算子名（不是自己）有什么用？
**答案**：声明连带验证。文件头注释明确「有些算子在 examples 用例中需要一起执行」，例如 A 依赖 B 的输出，A 的 example 会调 B，那么 A 的 src 变更也应触发 B 的验证；且「被触发结点的 options 不会递归触发」，避免雪崩式扩散（[tests/test_config.yaml:38-46](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_config.yaml#L38-L46)）。

### 4.3 --PR_UT：按变更裁剪的 UT 与 SoC 矩阵

#### 4.3.1 概念说明

拿到「要测的算子集合」后，`--PR_UT` 分支负责把它变成实际的 UT 执行。这里有两个工程要点：一是 **mc2 拆批**——mc2 算子的 UT 依赖通信库与多 rank 环境，跑法与其他算子不同，所以拆成两批分别构建；二是 **SoC 矩阵**——非 mc2 算子固定在 ascend310p/ascend910b/ascend950 三 SoC 上测，mc2 算子的 SoC 集合则由 `test_soc_config.yaml` 按变更文件动态决定。

#### 4.3.2 核心流程

```text
--PR_UT <changed_files>
  ├─ 解析 yaml → TEST_MC2（mc2 算子集）/ TEST_EXCLUDE_MC2（非 mc2 算子集）
  ├─ 解析 soc yaml → UT_SOC_ARRAY（mc2 批的 SoC 列表）
  ▼ main() 分发（PR_UT_FLAG=TRUE）
  ├─ build_pr_ut_exclude_mc2()   固定 ascend310p,ascend910b,ascend950 一轮构建
  └─ build_pr_ut_mc2()           逐 SoC 循环构建；首轮追加 infershape UT + opapi UT
```

#### 4.3.3 源码精读

`--PR_UT` 分支本体——三次 Python 调用决定整场测试的边界：

```bash
--PR_UT)
    PR_CHANGED_FILES="$2"
    ENABLE_TEST=TRUE
    PR_UT_FLAG=TRUE
    TEST_MC2=$(python3 .../parse_changed_files.py -c tests/test_config.yaml -f "$PR_CHANGED_FILES" get_related_ut_mc2)
    TEST_EXCLUDE_MC2=$(python3 .../parse_changed_files.py -c tests/test_config.yaml -f "$PR_CHANGED_FILES" get_related_ut_exclude_mc2)
    ut_soc_version=$(python3 .../get_soc_version.py -c tests/test_soc_config.yaml -f "$PR_CHANGED_FILES" get_related_soc)
    ...
    CI_MODE=TRUE
```

见 [build.sh:1732-1745](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1732-L1745)。注意它是「解析即执行」风格：参数还没解析完，裁剪计算已经完成，结果存进 shell 变量供 main() 使用。

mc2 批的逐 SoC 构建：

```bash
CUSTOM_OPTION="${CUSTOM_OPTION} -DTESTS_UT_OPS_TEST_CI_PR=ON"
CUSTOM_OPTION="${CUSTOM_OPTION} -DTESTS_UT_OPS_TEST=${TEST_MC2}"
for element in "${UT_SOC_ARRAY[@]}"; do
    if [ $UT_TEST_CNT -eq 0 ]; then
        CUSTOM_OPTION="${CUSTOM_OPTION} -DUT_INFERSHAPE_FLAG=TRUE"
        CUSTOM_OPTION="${CUSTOM_OPTION} -DOP_API_UT=TRUE"
    else
        ...FALSE...
    fi
    process_soc_input "$element"
    set_compute_unit_option_ut
    build_ut ${BUILD}
    UT_TEST_CNT=$((UT_TEST_CNT +1))
done
```

见 [build.sh:2224-2252](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2224-L2252)。解读三个细节：

- 空集合直接跳过并打日志「This PR didn't trigger any mc2 UTest」——纯 attention 的 PR 不会空跑 mc2 流程；
- 集合为 `all` 时不追加 `-DASCEND_OP_NAME`，即不裁剪、全量构建；
- **只在第一个 SoC 上跑 infershape UT 与 opapi UT**（`UT_TEST_CNT -eq 0` 分支）：这两类 UT 与芯片无关（u7-l1 讲过 opapi UT 用 stub 只测第一段），没必要在三个 SoC 上重复跑——这是裁剪思想在测试类型维度的又一次应用。

非 mc2 批则简单得多，SoC 写死为三件套：

```bash
process_soc_input "ascend310p,ascend910b,ascend950"
```

见 [build.sh:2254-2271](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2254-L2271)。main() 里的分发顺序是先 exclude_mc2 后 mc2，见 [build.sh:2290-2298](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2290-L2298)。

此外 scripts/ci 下还有按 SoC 组织的算子清单，用于真机 ST（系统级测试）把算子分组并行调度：

```yaml
operator_group_1:
  - mla_prolog
  - mla_prolog_v2
  - fused_infer_attention_score,10-8
  ...
```

见 [scripts/ci/ascend950/ops_transformer_operator_list.yaml:11-40](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/ci/ascend950/ops_transformer_operator_list.yaml#L11-L40)（ascend910b 目录下有同名文件，构成 910b/950 两组矩阵）。条目中「算子名,数字-数字」后缀的具体含义（疑似分片编号）**待确认**，但分组结构清晰：同域算子聚在一组，组间可并行。

#### 4.3.4 代码实践

1. **实践目标**：用 `--noexec`（只编译不执行）验证 `--PR_UT` 的裁剪确实生效。
2. **操作步骤**：
   - 准备只含一行 `attention/flash_attention_score/op_host/flash_attention_score_tiling.cpp` 的 `changed.txt`；
   - 执行 `bash build.sh --PR_UT ./changed.txt --noexec`（需要本地已装 CANN toolkit；无 NPU 也可编译，见 u1-l3 编译态概念）；
   - 观察终端开头的 `UT_SOC_ARRAY = ...` 日志与 cmake 配置行的 `-DTESTS_UT_OPS_TEST` / `-DASCEND_OP_NAME` 值。
3. **需要观察的现象**：`TEST_MC2` 为空时先打印「This PR didn't trigger any mc2 UTest」；exclude_mc2 批的 `TESTS_UT_OPS_TEST` 只含 `flash_attention_score` 而非全量算子。
4. **预期结果**：UT 编译目标数远小于全仓算子数，构建时间明显短于 `bash build.sh -u`（全量 UT）。待本地验证（取决于机器环境与 CANN 版本配套）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 mc2 批要逐 SoC 循环而不能像非 mc2 批那样一次传三个 SoC？
**答案**：mc2 算子的 tiling 与通信行为和 SoC 架构强相关（u5-l3 的 commMode 差异、u5-l4 的 v2/v3 SoC 路由），且 `test_soc_config.yaml` 允许按变更文件给出任意 SoC 子集；逐个构建使每次 cmake 配置对应单一 `ASCEND_COMPUTE_UNIT`，产物隔离清晰（[build.sh:1738-1741](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1738-L1741) 的 `UT_SOC_ARRAY` 来源）。
**练习 2**：UT 结果怎么判定通过/失败？
**答案**：承接 u7-l1 的结论——expectTilingData 断言 tiling 字段、错误注入断言返回码；CI 侧以 `build_ut` 的退出码为门禁信号，任何用例非零退出即门禁失败。

### 4.4 --PR_PKG 与冒烟：按变更裁剪的出包与 example 执行

#### 4.4.1 概念说明

UT 之外的第二、三条 CI 通道：`--PR_PKG` 按「变更算子」编译自定义安装包（验证出包链路可用），`-f/--changed_list` 配合 `--run_example` 做冒烟测试（把改动算子的 example 真正编译运行一遍，证明端到端可用）。两者都复用 4.2 的解析引擎。

#### 4.4.2 核心流程

```text
--PR_PKG <changed_files>
  ├─ experimental 场景 → scripts/ci/parse_changed_ops.py 提取算子名
  ├─ 普通场景 → parse_changed_files.py get_related_examples
  ├─ 结果为空 → log "No custom packages to build" → exit 200（CI 识别为跳过）
  └─ 算子集 → ENABLE_BUILD_PKG + ENABLE_BUILT_CUSTOM（cust 自定义包模式）

--run_example -f <changed_files>
  └─ process_ci_smoke_with_changed_list：逐算子 build_example_for_ci
```

#### 4.4.3 源码精读

`--PR_PKG` 分支：

```bash
--PR_PKG)
    PR_CHANGED_FILES="$2"
    if [[ "$ENABLE_EXPERIMENTAL" == "TRUE" ]]; then
        parse_changed_files            # 内部调 scripts/ci/parse_changed_ops.py
    else
        ops_names=$(python3 .../parse_changed_files.py ... get_related_examples)
    fi
    echo "Operators that need custom package compilation:$ops_names"
    if [ -z "${ops_names}" ]; then
        log "Info: No custom packages to build for this PR."
        exit 200
    fi
    ops_names="${ops_names%;}"
    ops_names="${ops_names//;/,}"
    ascend_op_name="$ops_names"
    ENABLE_BUILD_PKG=TRUE
    ENABLE_BUILT_CUSTOM=TRUE
    ENABLE_BUILT_IN=FALSE
```

见 [build.sh:1746-1767](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1746-L1767)。三个值得注意的设计：`exit 200` 是与 CI 约定的「无产物但非失败」退出码；分号串转逗号串是因为 yaml 解析器输出 `a;b;` 而 cmake 的 `ASCEND_OP_NAME` 吃逗号分隔（u1-l4 讲过该变量）；`ENABLE_BUILT_CUSTOM=TRUE + ENABLE_BUILT_IN=FALSE` 组合把构建切到 cust 自定义包通道（`pack_custom`，见 4.5.3）。

experimental 场景走的是另一套解析器 `scripts/ci/parse_changed_ops.py`：它按路径段提取「域/算子名」（`attention/xxx` → domain=attention, op=xxx），维护一份 BlackList（`fused_infer_attention_score` 等暂不按算子粒度出包的名字），并对 experimental 目录做存在性检查——详见 [scripts/ci/parse_changed_ops.py:37-80](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/ci/parse_changed_ops.py#L37-L80)。仓库在 `build.sh` 里对此的封装见 [build.sh:1204-1217](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1204-L1217)。

冒烟通道的入口参数：

```bash
-f|--changed_list)
    PR_CHANGED_FILES="$2"
    ENABLE_SMOKE=TRUE
    PKG_MODE="cust"
    vendor_name="custom"
    CI_MODE=TRUE
```

见 [build.sh:1724-1731](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1724-L1731)——冒烟直接用 cust 包通道现场编译 example，不依赖已安装的内置包。冒烟主函数：

```bash
function process_ci_smoke_with_changed_list()
{
    TEST=$(python3 .../parse_changed_files.py ... get_related_examples)
    if [[ -z "$TEST" ]];then
        echo "No related unit tests found. Skipping CI test execution."
        exit 0
    fi
    IFS=';' read -ra OPS_ARRAY <<< "$TEST"
    for op in "${OPS_ARRAY[@]}";do
        build_example_for_ci "$op"
    done
}
```

见 [build.sh:2186-2206](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2186-L2206)：还是同一个解析器、同一个 `get_related_examples` 子命令——UT 裁剪与冒烟裁剪共享一份事实来源，配置只写一遍。

#### 4.4.4 代码实践

1. **实践目标**：走读并画出「提交 PR → 解析变更算子 → 按需 UT/出包」完整流程图。
2. **操作步骤**：
   - 通读本讲引用的 [build.sh:1732-1767](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1732-L1767) 与 [build.sh:2186-2298](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2186-L2298)；
   - 用 4.2.4 的 `changed2.txt` 分别跑 `get_related_ut_exclude_mc2`、`get_related_ut_mc2`、`get_related_examples`，把三个输出填进下图的空框：
     ```text
     PR 变更文件清单
        │
        ├─► get_related_ut_exclude_mc2 ──► 【____】─┐
        ├─► get_related_ut_mc2 + SoC解析 ──► 【____】─┤─► build_pr_ut_exclude_mc2 / build_pr_ut_mc2
        ├─► get_related_examples ──► 【____】────────┼─► --PR_PKG: cust 出包（空则 exit 200）
        └─► get_related_examples ──► 同上 ────────────┴─► 冒烟: 逐算子 build_example_for_ci
     ```
3. **需要观察的现象**：三个子命令输出的是「同一份 yaml 推导出的三个投影」，没有任何一处重复配置。
4. **预期结果**：流程图中每个箭头都能对应到本讲引用的一条真实代码行；把图存进自己的学习笔记。

#### 4.4.5 小练习与答案

**练习 1**：`exit 200` 为什么不用 `exit 0`？
**答案**：`exit 0` 表示「检查通过」，`exit 1` 表示失败；200 是一个非零非典型错误码，CI 平台可将其配置为「跳过（skipped）」而非失败——只改文档的 PR 就会走到这里，门禁应显示跳过而不是绿色通过。
**练习 2**：`--PR_PKG` 与 `--run_example -f` 都会编译 example 相关产物，为什么是两条通道？
**答案**：前者验证的是**出包链路**（把变更算子打进 cust .run 包，产出物是安装包），后者验证的是**运行链路**（example 编译+执行，产出物是运行结果）。一个面向部署正确性，一个面向功能正确性。

### 4.5 包管理与版本发布：run / rpm / deb 与 version.cmake

#### 4.5.1 概念说明

CI 验证通过后，仓库需要产出可安装的发布物。`--pkg-type` 提供四种取值：`run`（默认，makeself 自解压包，与 CANN 生态的 `bash Ascend-cann-*.run` 安装体验一致）、`rpm`（RedHat 系包管理器格式）、`deb`（Debian 系格式）、`all`（三种全出）。版本管理则由 `version.cmake` 集中声明：本组件版本号、以及构建期/运行期对 CANN 各子包的最低版本要求。

#### 4.5.2 核心流程

```text
bash build.sh --pkg --pkg-type=<TYPE> --soc=...
  └─ check_pkg_type 校验取值 ∈ {run, rpm, deb, all}
  └─ check_param 组合约束校验（rpm/deb 只允许内置包）
  └─ assemble_cmake_args: -DPACKAGE_TYPE=...（all 先按 run 传给 cmake）
  └─ cmake/CPack 打包
       ├─ TYPE=run/rpm/deb → 一次 CPack 出对应包
       └─ TYPE=all → build_package 循环: run → rpm → deb 逐个重配 cmake 再打包
  └─ collect_rpm_deb_package 把 .rpm/.deb 拷到 build_out/
```

#### 4.5.3 源码精读

帮助信息与校验：

```bash
echo "    --pkg                  Build run package with kernel bin"
echo "    --pkg-type=<TYPE>      Specify package type(TYPE options: run/rpm/deb/all), Default: run"
```

见 [build.sh:107-108](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L107-L108)，示例命令见 [build.sh:130-132](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L130-L132)。取值白名单在两处双重把关——shell 侧 `check_pkg_type`（[build.sh:408-414](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L408-L414)）与 cmake 侧缓存变量校验：

```cmake
set(PACKAGE_TYPE "run" CACHE STRING "package type: run/rpm/deb/all")
if(NOT PACKAGE_TYPE IN_LIST SUPPORTED_PACKAGE_TYPES)
  message(FATAL_ERROR "PACKAGE_TYPE only supports run/rpm/deb/all, got: ${PACKAGE_TYPE}")
```

见 [CMakeLists.txt:64-68](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L64-L68)。组合约束（[build.sh:1992-2010](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1992-L2010)）有三条：`--pkg-type` 必须伴随 `--pkg`；rpm/deb 不能与 `--static`/`--jit` 组合；**rpm/deb 只支持内置 ops-transformer 包**，不允许 `--ops`/`--vendor_name`/`--experimental`——自定义算子包只有 run 一种形态。

`all` 类型的循环打包：

```bash
function build_package(){
    if [[ "${PACKAGE_TYPE}" == "all" ]]; then
        for PACKAGE_TYPE in run rpm deb; do
            clean_rpm_deb_package
            cmake -DPACKAGE_TYPE="${PACKAGE_TYPE}" "${BUILD_PATH}" > /dev/null 2>&1
            cmake --build . --target package ${JOB_NUM} ${option}
            collect_rpm_deb_package
        done
    ...
```

见 [build.sh:800-823](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L800-L823)：`all` 在 cmake 层永远先按 `run` 配置（[build.sh:1538-1542](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1538-L1542)），再在 shell 层循环改 `PACKAGE_TYPE` 重跑 cmake；`collect_rpm_deb_package` 把产物统一拷进 `build_out/`（[build.sh:776-798](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L776-L798)）。

CMake 侧两个出包函数（[cmake/package.cmake:15-54](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/package.cmake#L15-L54) 与 [cmake/package.cmake:56-156](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/package.cmake#L56-L156)）：

- `pack_custom`：拉取 makeself 三方件后用 `npu_op_package(... TYPE RUN ...)` 产出形如 `cann-ops-transformer-${VENDOR_NAME}-linux-${ARCH}` 的自定义 run 包，收录 `cust_opapi`/`cust_proto`/`cust_opmaster` 库——正是 `--PR_PKG` 与 `--vendor_name` 走的通道；
- `pack_built_in`：内置包通道。安装 setenv/prereq_check 脚本与安装器公共件，把 `version.info`（由 `version.cmake` 数据生成）与 torch_extension 的 whl（[cmake/package.cmake:130-140](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/package.cmake#L130-L140)）一并入包，最后一句交给 CPack：

```cmake
set_cann_cpack_config(ops-transformer COMPUTE_UNIT ${compute_unit}
                      SHARE_INFO_NAME ops_transformer PACKAGE_TYPE "${PACKAGE_TYPE}")
```

见 [cmake/package.cmake:154-155](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/package.cmake#L154-L155)。`set_cann_cpack_config` 来自 CANN 包提供的公共 cmake，rpm/deb 的包元数据也在这里按 PACKAGE_TYPE 分支生成（其实现位于安装的 CANN 包内，不在本仓库，待确认细节）。

版本与依赖声明，整个发布物的「户口本」：

```cmake
set_cann_package(ops-transformer VERSION "9.1.0")

set_cann_build_dependencies(runtime ">=8.5")
set_cann_build_dependencies(opbase ">=8.5")
...
set_cann_run_dependencies(runtime ">=8.5")
...
set_cann_run_dependencies(ops-nn ">=8.5")
set_cann_run_dependencies(hccl ">=8.5")
```

见 [version.cmake:11-32](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/version.cmake#L11-L32)。解读：

- `VERSION "9.1.0"` 即本仓库当前开发的 CANN 大版本线，与 u1-l3 讲的「源码标签配套 CANN 版本」互为印证；
- **构建依赖**（runtime/opbase/hcomm/ge-executor/metadef/ge-compiler/asc-devkit/bisheng-compiler）与**运行依赖**（额外多出 asc-tools/ops-nn/hccl）分开声明——编译一台机器和部署一台机器需要装的东西不同；
- CMakeLists.txt 在配置期就会消费这些声明做兼容性检查：`check_cann_pkg_build_deps(${CANN_VERSION_PACKAGES})`（[CMakeLists.txt:100-102](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L100-L102)），环境 CANN 版本过低会在 cmake 阶段直接报错，而不是编到一半才炸。

还有一个兜底细节：当传入不支持的芯片时，仓库不报错退出，而是打出**空包**：

```cmake
if ((NOT BUILD_OPS_RTY_KERNEL) AND (BUILD_OPEN_PROJECT))
    include(cmake/build_empty_package.cmake)
    cpack_empty_package()
    return()
```

见 [CMakeLists.txt:108-120](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L108-L120)。这让 CI 矩阵里「某芯片本轮不可用」时流水线不至于红——空包是合法产物。

#### 4.5.4 代码实践

1. **实践目标**：用 `--pkg-type` 的帮助与校验逻辑，整理三种包型的差异表。
2. **操作步骤**：
   - 运行 `bash build.sh --help package`，阅读 `--pkg` / `--pkg-type` / `--jit` 三行；
   - 运行 `bash build.sh --pkg --pkg-type=deb --ops=flash_attention_score --soc=ascend910b`，**预期直接报错**，读错误信息；
   - 运行 `bash build.sh --pkg --pkg-type=tar --soc=ascend910b`，同样预期被 `check_pkg_type` 拦截（[build.sh:408-414](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L408-L414)）；
   - 把观察结果填进下表：

     | 包型 | 产物格式 | 安装方式 | 允许 --ops/--vendor_name | 典型场景 |
     | --- | --- | --- | --- | --- |
     | run | makeself 自解压脚本 | `bash xxx.run` | 是 | 个人算子包、自定义包、CI cust 包 |
     | rpm | .rpm | `rpm -i` | 否 | RedHat 系批量部署 |
     | deb | .deb | `dpkg -i` | 否 | Debian/Ubuntu 系批量部署 |

3. **需要观察的现象**：第二条命令的错误正是 [build.sh:2007](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2007) 的「only supports built-in ops-transformer packages」。
4. **预期结果**：错误信息与源码逐字对应，表格完成。`--help` 类命令无需 NPU；若手头有配套环境，可再跑一次不带 `--ops` 的 `--pkg --pkg-type=deb` 观察 `build_out/` 下生成的 .deb 文件名（含版本号 9.1.0 线）。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `--pkg-type=all` 传给 cmake 的是 `run` 而不是 `all`？
**答案**：cmake 的 `PACKAGE_TYPE` 白名单里有 `all`（[CMakeLists.txt:66](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L66)），但单次 CPack 只能出一种包；`all` 的语义由 shell 层的 `for PACKAGE_TYPE in run rpm deb` 循环实现（[build.sh:807-816](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L807-L816)），首轮配置用 `run` 完成主编译即可。
**练习 2**：torch_extension 的 whl 是怎么进安装包的？
**答案**：`pack_built_in` 里 `file(GLOB ... cann_ops_transformer-*.whl)` 从 `torch_extension/dist` 收集，存在则安装到 `${WHL_INSTALL_DIR}/es_packages/whl`（[cmake/package.cmake:130-140](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/package.cmake#L130-L140)）；所以想让它入包，需先 `--torch_extension_only` 构建 whl（u3-l3）再出包。
**练习 3**：某台 CI 机器装了 CANN 8.2，构建会怎样？
**答案**：`version.cmake` 声明构建依赖 `>=8.5`，`check_cann_pkg_build_deps` 在 cmake 配置期校验失败直接终止（[CMakeLists.txt:100-102](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L100-L102)），避免编到链接期才因符号缺失报难懂的错。

## 5. 综合实践

**任务：给一个「假想 PR」做完整的 CI 推演，并验证一遍出包约束。**

背景：假设你按 u6-l1 开发了一个新算子 `examples/my_sum`，并且不小心把 `attention/flash_attention_score/docs/aclnnFlashAttentionScore.md` 也改了。请完成：

1. **登记看护**：在 `tests/test_config.yaml` 的合适层级为 `my_sum` 新增结点（src 看护 `examples/my_sum`，exclude 排除其 docs 与 README，options 填 `my_sum`）。（本步骤只改 yaml 做实验，实验后请还原。）
2. **构造变更清单** `changed.txt`，包含：
   ```text
   examples/my_sum/op_host/my_sum_tiling.cpp
   attention/flash_attention_score/docs/aclnnFlashAttentionScore.md
   ```
3. **推演并验证**：先在纸上写出你预期的 `get_related_ut` / `get_related_examples` 输出，再实际运行两个子命令比对。思考：docs 下文档的变更为什么两个命令都「看不见」？（提示：`file_filter` 的 `docs/` 关键字与结点 `exclude` 双重作用。）
4. **验证 SoC 裁剪**：把 `mc2/common/src/fallback/fallback_comm.cpp` 加入清单，运行 `cmake/scripts/get_soc_version.py -c tests/test_soc_config.yaml -f changed.txt get_related_soc`，确认输出是三 SoC 全集（对应 [tests/test_soc_config.yaml:14-22](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/tests/test_soc_config.yaml#L14-L22) 的 all_soc 结点）。
5. **出包约束三连**：依次执行并解释每条结果——
   - `bash build.sh --pkg --pkg-type=tar --soc=ascend910b`（白名单拦截）
   - `bash build.sh --pkg-type=deb`（缺 `--pkg` 拦截）
   - `bash build.sh --pkg --pkg-type=deb --ops=flash_attention_score`（自定义包与 rpm/deb 互斥拦截）
6. **产出**：一幅完整流程图（提交 PR → changed files → test_config/test_soc_config 解析 → --PR_UT 两批 / --PR_PKG cust 包 / 冒烟 example → 门禁四项 → 合入），以及一张 run/rpm/deb 差异表。

整个实践除第 5 步的 `--help` 级别命令外均不需要 NPU；步骤 1~4 是纯 Python/yaml 操作。完成后记得用 `git checkout -- tests/test_config.yaml` 还原实验性修改。

## 6. 本讲小结

- CI 门禁四项（编译/静态检查/UT/冒烟）全部收敛到 `build.sh` 的三个 PR 分支：`--PR_UT`、`--PR_PKG`、`-f/--changed_list`，它们以 `PR_CHANGED_FILES` 是否为空标识 PR 场景。
- 裁剪引擎 = `tests/test_config.yaml`（结点声明 src 看护/ exclude 排除/options 连带触发）+ `cmake/scripts/parse_changed_files.py`（路径前缀匹配 + `.md/.json/.ini/docs` 过滤），四个子命令输出 UT 全量、UT-mc2、UT-非mc2、example 四个投影；`tests/test_soc_config.yaml` 以同构方式裁剪 SoC 矩阵。
- `classify_rule.yaml` 管「人/组件/交付范围」（huawei_style/opensource_style、unrelease 的 tests/examples/docs），`test_config.yaml` 管「测试触发」，二者互补、互不替代。
- mc2 因依赖通信库被拆成独立 UT 批：非 mc2 批固定三 SoC、首轮 SoC 才跑 infershape/opapi UT；mc2 批按 yaml 动态 SoC 逐个构建。
- 包型四种取值 run/rpm/deb/all 在 shell 与 cmake 双重白名单校验；rpm/deb 仅限内置包，自定义包（--ops/--vendor_name/--experimental）只有 run 形态；`all` 由 shell 循环重配 cmake 实现。
- 版本管理集中在 `version.cmake`：本组件 9.1.0，构建/运行依赖分开声明（>=8.5），配置期由 `check_cann_pkg_build_deps` 前置校验；不支持的芯片产空包兜底而非报错。

## 7. 下一步学习建议

- 下一讲 u7-l3「贡献流程与代码规范」会把本讲的门禁放进完整社区协作链路：fork-branch-PR、PR 模板、pre-commit 与 OAT 开源合规检查。
- 想深挖打包细节，可读 `cmake/package.cmake` 引用的 `scripts/package/ops_transformer/`（含 rpm/deb 的 postinst/prerm 维护脚本）与 CANN 包安装目录下的 `set_cann_cpack_config` 实现。
- 想理解 UT 在 cmake 层如何被 `TESTS_UT_OPS_TEST` 裁剪，回到 `tests/ut` 目录与 `cmake/func_utest.cmake` 对照 u7-l1 的用例收集约定。
- 最后一讲 u7-l4 将做全仓库架构复盘，本讲的「按需裁剪、单一事实来源、版本配套」三个工程决策都是复盘素材。
