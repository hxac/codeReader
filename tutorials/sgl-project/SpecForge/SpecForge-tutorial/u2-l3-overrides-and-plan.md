# 命令行覆盖与 plan 预览

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `section.field=value`（点分覆盖）语法在不复制 YAML 的情况下临时改一个训练参数。
- 说清楚一条覆盖从命令行到最终生效所经过的三道关卡：路径校验、标量强制转换、整体重校验。
- 解释「未知字段直接报错」这件事在配置文件和命令行覆盖两条路径上分别由谁负责、为什么 SpecForge 选择 fail-fast。
- 用 `--plan` 在不占 GPU、不启动任何 worker 的前提下，预览一次 run 会被拆成哪些进程、用什么命令拉起。

本讲只关心「配置怎么被改、怎么被检查、怎么被预览」，不涉及模型装配和训练循环本身。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：YAML 是 run 的契约，不是脚本。**
SpecForge 全项目只有一个训练入口 `specforge train --config run.yaml`（见 [u2-l1](u2-l1-first-run.md)）。一次 run 的草稿方法、目标模型、数据源、优化器、部署拓扑全部写死在这一份类型化 YAML 里，没有方法专属的 Python 入口。既然 YAML 是「契约」，那「临时改一个值再跑」就必须是受控的、可校验的操作，而不是随手改文件。

**直觉二：Pydantic 的「严格模式」。**
SpecForge 的配置类继承自 `StrictConfigModel`，它的核心设定是 `extra="forbid"`：你在 YAML 里多写一个字段（哪怕只是拼错了一个字母），Pydantic 会直接抛 `ValidationError`，而不是悄悄忽略。这一点是后面「未知字段报错」的根。我们上一讲（[u2-l2](u2-l2-config-sections.md)）已经看过七段结构，本讲聚焦「覆盖」与「预览」两条边路。

**直觉三：标量会被「重新校验」时强制转换。**
你在命令行里写的 `training.max_steps=10`，那个 `10` 一开始是个**字符串** `"10"`。SpecForge 并不会自己去 `int()`，而是把覆盖写回配置字典后，让 Pydantic 在最终 `model_validate` 时按字段声明的类型（这里是 `Optional[int]`）做强制转换（coercion）。理解这一点，才能解释为什么 `training.learning_rate=5e-5` 这种科学计数法写法也能被正确解析成浮点数。

## 3. 本讲源码地图

本讲涉及三个关键文件，各管一段：

| 文件 | 在本讲的职责 |
| --- | --- |
| [`specforge/cli.py`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py) | 定义 `--plan` 开关、接收 `overrides` 位置参数；把 `args.config` 和 `args.overrides` 交给 `load_config`；决定是「打印计划」还是「真正起进程」。 |
| [`specforge/config/schema.py`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py) | 定义 `StrictConfigModel`（`extra="forbid"`）、`apply_overrides`、`load_config`、`Config.from_file`、`migrate_legacy_config`。本讲的「肌肉」都在这里。 |
| [`specforge/launch_plan.py`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py) | 定义 `LaunchPlan` 及其 `render()` 方法、`build_launch_plan()` 构造器。`--plan` 打印的就是 `LaunchPlan.render()` 的输出。 |
| [`docs/basic_usage/training.md`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md) | 官方文档对「覆盖语法」「未知字段报错」的说明，是本讲叙述的事实依据之一。 |

本讲不深入 `build_launch_plan` 内部如何区分 producer/consumer/worker（那是 [u3-l2](u3-l2-launch-plan.md) 的主题），只关注 `--plan` 这条「预览出口」。

## 4. 核心概念与源码讲解

### 4.1 命令行覆盖 apply_overrides

#### 4.1.1 概念说明

「覆盖」（override）解决一个很具体的问题：你想临时改一个参数跑一枪，但不想复制一份新 YAML、也不想去改那份受版本管理的黄金样例。比如官方仓库里的 [`examples/configs/qwen3-8b-eagle3-disaggregated.yaml`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml) 写死了 `max_steps: 10000`，你只想跑 10 步做个冒烟测试。

SpecForge 的做法是在命令行末尾追加若干 `section.field=value` 形式的位置参数：

```bash
specforge train --config examples/configs/qwen3-8b-eagle3-disaggregated.yaml \
  training.max_steps=10 \
  training.learning_rate=5e-5 \
  output_dir=./outputs/eagle3-smoke
```

负责「把这些字符串变成配置」的函数就是 `apply_overrides`。它的关键设计有三点：

1. **覆盖的是已校验过的配置**，不是原始 YAML 文本。它先把一份合法的 `Config` 对象 `model_dump` 成纯字典，再在字典上改。
2. **路径必须真实存在**。写错了路径（比如把 `learning_rate` 拼成 `learnig_rate`）会直接报错。
3. **改完要整体重校验**。覆盖不会绕过类型检查和跨字段校验。

#### 4.1.2 核心流程

`apply_overrides(config, overrides)` 对每一条覆盖字符串执行以下步骤：

```
1. raw = config.model_dump()            # 把已校验 Config 拍平成纯 dict
2. 对每一条 item in overrides:
   a. 若不含 "="          → 报错：not of the form path=value
   b. path, value = item.split("=", 1)  # 只切第一个等号
   c. 沿 path.split(".") 走字典树：
        - 每一个中间 key 必须指向 dict，否则报错：does not exist
   d. 最后一个 key 必须已存在于当前 node，否则报错：does not exist
   e. 取当前值 current：
        - 若 current 是 dict/list 且 value 以 "[" 或 "{" 开头
          → 用 yaml.safe_load 解析成结构化值（列表/字典）
        - 否则 → 原样把字符串写进去，留给最终的 model_validate 做标量强制转换
   f. node[keys[-1]] = 赋值后的值
3. return Config.model_validate(raw)    # 整体重校验，返回新的 Config
```

两条最容易踩的报错都在第 2c/2d 步：**路径不存在**。这正是「未知覆盖字段报错」的实现位置——拼写错误、已废弃字段都会在这里被拦下。

#### 4.1.3 源码精读

先看 CLI 如何声明 `overrides` 为「零个或多个位置参数」，这是覆盖语法的入口：

[specforge/cli.py:194-198](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L194-L198) —— `nargs="*"` 表示所有 `section.field=value` 形式的尾部参数都会被收进 `args.overrides` 列表，且 argparse 不会因为没给覆盖而报错（冒烟测试可以一个覆盖都不加）。

真正干活的是 `apply_overrides`：

[specforge/config/schema.py:892-918](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L892-L918) —— 逐行对应上面的流程。重点看几处：

- 第 896-897 行：`if "=" not in item` → 任何不含等号的参数（比如误把 `--config` 写错位置）会被明确拒绝，而不是被当成「值」。
- 第 900-904 行：遍历 `keys[:-1]`（路径上除最后一层外的所有 key），逐层用 `isinstance(node.get(key), dict)` 校验「这条路走得通」。
- 第 905-906 行：最后一个 key 用 `keys[-1] not in node` 校验，不存在即报错。**这就是「未知覆盖路径报错」的精确落点。**
- 第 908-916 行：结构化值（以 `[` 或 `{` 开头）走 `yaml.safe_load`；例如你可以写 `deployment.disaggregated.server_urls='["http://127.0.0.1:30000"]'` 来整体替换一个列表字段。
- 第 917 行注释 `# pydantic coerces scalars on re-validation`：标量字符串原样写入，类型转换交给第 918 行的 `Config.model_validate(raw)`。

第 918 行是整个函数的「收口」：无论改了多少个字段，都通过一次完整的 `Config.model_validate` 重新校验。这意味着覆盖不会破坏跨字段约束（例如 `data.eval_hidden_states_path` 必须与 `training.eval_interval` 同时出现的规则依然生效）。

#### 4.1.4 代码实践（源码阅读 + 行为预测）

**实践目标**：在不运行的情况下，精确预测 `training.max_steps=10` 走完 `apply_overrides` 后发生了什么，以及一个拼写错误会怎样失败。

**操作步骤**：

1. 打开 [`examples/configs/qwen3-8b-eagle3-disaggregated.yaml`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml)，确认 `training` 段里存在 `max_steps: 10000` 和 `learning_rate: 1.0e-4` 两个字段。
2. 对照 `apply_overrides`（[schema.py:892-918](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L892-L918)），手推 `training.max_steps=10` 的执行轨迹：
   - `path="training.max_steps"`，`value="10"`，`keys=["training","max_steps"]`。
   - 中间层 `keys[:-1]=["training"]`：`raw["training"]` 是 dict，通过。
   - 末层 `"max_steps" in raw["training"]` 为真（值原本是 10000），通过。
   - `current=10000` 是 int、不是 dict/list 且 value `"10"` 不以 `[/{` 开头 → 走标量分支，`raw["training"]["max_steps"] = "10"`（注意此刻还是字符串）。
   - 最后 `Config.model_validate(raw)` 把字符串 `"10"` 按 `Optional[int]` 强制转换成整数 `10`。
3. 再手推一个**故意拼错**的覆盖 `training.learnig_rate=5e-5`：
   - 末层 `"learnig_rate" in raw["training"]` 为假 → 抛 `ValueError("override path 'training.learnig_rate' does not exist")`。

**需要观察的现象**：

- 正确覆盖：配置字典里 `max_steps` 被替换，且最终被强转为 `int`；不影响其他字段。
- 拼写错误：在 `model_validate` 之前就抛出 `ValueError`，且错误信息明确指出「哪条路径不存在」。

**预期结果**：覆盖只改命中字段；任何不存在的路径都会以 `ValueError` 立即终止，不会产生「静默忽略」。

> 关于 `learning_rate=5e-5` 能否被解析为浮点数：由于走的是 Pydantic 在 `model_validate` 时的浮点强制转换，而 `5e-5` 是合法的浮点字面量字符串，最终应为 `5e-5`。**具体到你的 Pydantic 版本是否接受科学计数法字符串，待本地验证**（可用下一节的实践命令实地确认）。

#### 4.1.5 小练习与答案

**练习 1**：如果用户写了 `training.batch_size`（漏了 `=value`），会发生什么？
**答案**：`item` 不含 `=`，命中 [schema.py:896-897](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L896-L897)，抛 `ValueError("override ... is not of the form path=value")`。

**练习 2**：为什么 `apply_overrides` 不自己写 `int(value)` / `float(value)` 来转换标量，而是把字符串原样塞回字典？
**答案**：因为单点转换无法覆盖跨字段校验。它选择在末尾做一次完整的 `Config.model_validate(raw)`（[schema.py:918](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L918)），让 Pydantic 统一负责类型强制转换和所有 `model_validator` 跨字段规则，避免「转换」与「校验」出现两套不一致的实现。

**练习 3**：想用一个覆盖把 `deployment.disaggregated.server_urls` 整体替换成一个新列表，该怎么写？
**答案**：写成结构化值，例如 `deployment.disaggregated.server_urls='["http://127.0.0.1:30001"]'`。由于当前值是 list 且 value 以 `[` 开头，会走 [schema.py:908-916](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L908-L916) 的 `yaml.safe_load` 分支解析成 Python 列表。

---

### 4.2 配置加载与校验 load_config

#### 4.2.1 概念说明

`apply_overrides` 处理「命令行覆盖」，`load_config` 则是它和 YAML 文件共同的「入口与收口」。它回答两个问题：

1. 一份磁盘上的 YAML/JSON 是怎么变成一个类型化 `Config` 对象的？
2. 为什么「未知字段」在这套体系里注定是错误，而不是被忽略？

第二个问题特别重要。在投机解码这种参数极多的训练框架里，字段名一旦拼错（或某个字段在新版本被废弃），如果框架默默忽略它，你会在「训练了半天、结果不对」之后才发现根因。SpecForge 的选择是 **fail-fast**：未知字段在配置加载阶段就报错。这背后是两个独立但配合的机制——YAML 里的未知字段由 `StrictConfigModel` 拦截，命令行覆盖里的未知路径由 `apply_overrides` 拦截（见 4.1）。

#### 4.2.2 核心流程

```
load_config(path, overrides):
  1. config = Config.from_file(path)
        a. 读文件：.yaml/.yml → yaml.safe_load；否则 → json.load
        b. migrate_legacy_config(raw)   # 兼容旧版 training.* 部署字段
        c. Config.model_validate(...)   # 第一次完整校验（extra="forbid" 在此生效）
  2. if overrides:
        config = apply_overrides(config, overrides)   # 内部会再做一次 model_validate
  3. return config
```

也就是说，一次带覆盖的 run，配置至少要过 **两次** `model_validate`：第一次在 `from_file`（校验原始 YAML），第二次在 `apply_overrides` 末尾（校验覆盖后的结果）。这保证了「先有一份合法的起点，再叠加受控的修改」。

#### 4.2.3 源码精读

先看「未知字段报错」的根——严格基类：

[specforge/config/schema.py:32-33](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L32-L33) —— 所有配置类（`ModelConfig`/`DataConfig`/`TrainingConfig`/`Config` 等）都继承自 `StrictConfigModel`，`ConfigDict(extra="forbid")` 让任何未声明字段在 `model_validate` 时触发 `ValidationError`。这就是「YAML 里写错字段名会报错」的总开关。

再看文件加载：

[specforge/config/schema.py:880-889](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L880-L889) —— `from_file` 是个 `classmethod`：按后缀选 YAML 或 JSON 解析器，然后**先过 `migrate_legacy_config` 再 `model_validate`**。注意顺序——迁移在前，校验在后，因此旧字段能被翻译成规范字段而不被 `extra="forbid"` 误杀。

`migrate_legacy_config` 是唯一的「兼容层」，只存在于这个原始加载边界：

[specforge/config/schema.py:578-641](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L578-L641) —— 它把旧的 `training.deployment_mode` / `training.server_urls` / `training.metadata_db_path` 翻译到规范的 `deployment.*` 段；若旧字段与显式写出的规范字段冲突（例如 `deployment.mode` 与 `training.deployment_mode` 不一致），也会直接 `raise ValueError`。注意：这层兼容只翻译、不回灌——返回的领域模型绝不会把规范的部署状态反向写回 `training`。

最后是入口函数本身：

[specforge/config/schema.py:921-925](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L921-L925) —— `load_config` 极简：`from_file` 之后，**仅当 `overrides` 非空**才调用 `apply_overrides`（无覆盖时不产生第二次校验，也不构造字典）。这与 CLI 调用点 [cli.py:242](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L242) 的 `cfg = load_config(args.config, args.overrides)` 完全对应。

> 顺带一提：CLI 里 `load_config` 直接消费的是 `args.overrides`（一个字符串列表），并没有提前做任何过滤——所以哪怕你传了 `--config` 之外的乱七八糟位置参数，最终都会进入 `apply_overrides` 被严格校验，不存在「悄悄丢弃」的尾部参数。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「未知字段直接报错」在两条路径上的表现。

**操作步骤**：

1. 在仓库根目录新建一个最小 YAML（示例代码，非项目自带文件），故意写一个不存在的字段：

   ```yaml
   # smoke-bad.yaml —— 示例文件，仅用于触发报错
   model:
     target_model_path: Qwen/Qwen3-8B
     target_backend: sglang
   data:
     train_data_path: ./cache/dataset/sharegpt_train.jsonl
   training:
     strategy: eagle3
     learnig_rate: 1.0e-4   # 故意拼错：应为 learning_rate
   ```

2. 运行（注意加 `--plan`，这样即便配置非法也会在校验阶段就报错，而不会去占 GPU）：

   ```bash
   specforge train --config smoke-bad.yaml --plan
   ```

3. 把字段名改对成 `learning_rate` 后再跑一次，确认报错消失（此时会进入 `--plan` 的正常输出，见 4.3）。

**需要观察的现象**：

- 第 2 步：抛出 Pydantic `ValidationError`，错误信息会明确指出 `training` 段出现了不被允许的字段（`extra` 字段），并给出字段名。
- 第 3 步：不再报字段错误（若其他必填项缺失，会报别的错误，那是另一回事）。

**预期结果**：`extra="forbid"` 让拼写错误无所遁形。错误信息的**精确措辞随 Pydantic 版本而变，待本地验证**，但「会明确点名那个多余字段」是确定的。

#### 4.2.5 小练习与答案

**练习 1**：一份 YAML 同时写了旧字段 `training.deployment_mode: disaggregated` 和规范字段 `deployment.mode: local_colocated`，加载结果是什么？
**答案**：`migrate_legacy_config`（[schema.py:606-610](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L606-L610)）检测到两者冲突，抛 `ValueError("deployment.mode conflicts with legacy training.deployment_mode")`。兼容层只做「无歧义翻译」，有歧义就报错。

**练习 2**：为什么带覆盖的 run 会过两次 `model_validate`？少一次行不行？
**答案**：`from_file` 的第一次校验保证「起点合法」（YAML 本身没错）；`apply_overrides` 末尾的第二次校验保证「覆盖后仍合法」（标量被正确强转、跨字段约束未被破坏）。少掉第二次，一个把 `learning_rate` 改成负数（违反 `gt=0.0`，见 [schema.py:489](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L489)）的覆盖就会带着非法值进入训练。

**练习 3**：`load_config` 在没有覆盖时，会不会构造中间字典再做 `model_validate`？
**答案**：不会。看 [schema.py:923-924](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L923-L924)，`if overrides:` 为假时直接返回 `from_file` 的结果，零开销。

---

### 4.3 进程计划预览 --plan

#### 4.3.1 概念说明

`--plan` 是一个「只看不动」的开关。它做完所有「解析配置、解析算法、推导进程拓扑」的工作，却在最后一步**打印计划、立刻返回**，既不起任何 worker 进程，也不初始化 CUDA 或分布式。这让你可以在一台没有 GPU 的机器上，先确认「这次 run 到底会被拆成几个进程、分别用什么命令拉起」。

这对 disaggregated 模式尤其有用：一次在线 run 会拉起 producer 和 consumer 两类进程（见 [u2-l1](u2-l1-first-run.md)），用 `--plan` 能在不真正连接 Mooncake/SGLang 服务的前提下，先看清这两条命令长什么样、环境变量怎么设。

#### 4.3.2 核心流程

在 CLI 的 `main()` 里，`train` 子命令的处理顺序是：

```
1. cfg = load_config(args.config, args.overrides)        # 加载 + 覆盖 + 校验
2. resolved = resolve_run(cfg)                            # 解析算法（按 training.strategy 查注册表）
3. plan = build_launch_plan(resolved.config, ...)         # 推导进程拓扑计划（纯计算，无副作用）
4. if args.plan:
       print(plan.render())                               # 打印 JSON 计划
       return 0                                           # 直接退出，不起进程
5. 否则按 plan.kind 走 worker 分支或 run_commands(plan)   # 真正执行
```

关键点：**第 4 步在第 5 步之前**。也就是说 `--plan` 复用了与真实训练完全相同的前三步（加载、解析、规划），只截断了「执行」。如果前三步里有任何配置错误，`--plan` 照样会报错——这让它成为一个免费的「配置体检」工具。

#### 4.3.3 源码精读

先看 `--plan` 开关本身：

[specforge/cli.py:189-193](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L189-L193) —— `action="store_true"`，默认 `False`，因此不加 `--plan` 时走正常训练路径。

再看 `main()` 里 `train` 分支的核心几行：

[specforge/cli.py:241-257](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L241-L257) —— 这段同时回答了三件事：

- 第 242 行：`load_config(args.config, args.overrides)` 把 YAML 与覆盖合并校验。
- 第 246-254 行：`resolve_run(cfg)` 解析算法、`build_launch_plan(...)` 构造 `plan`。注意 `build_launch_plan` 收到的 `overrides=args.overrides` 会被原样转发进子进程命令（见下）。
- 第 255-257 行：`if args.plan: print(plan.render()); return 0` —— 打印计划并返回，**不会**走到第 258 行的 `plan.kind == "worker"` 分支，也不会走到 [cli.py:267](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L267) 的 `run_commands(plan)`。

`plan.render()` 输出的是一段结构化 JSON：

[specforge/launch_plan.py:153-173](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L153-L173) —— `render()` 把 `LaunchPlan` 序列化成 JSON，核心字段是 `kind` / `role` / `commands` / `worker_env`；当 `kind == "managed_supervisor"` 时额外补上 `services` / `managed_root` / `managed_ports` / `shutdown_grace_s`。

值得强调的是 **`render` 会脱敏**。每条命令的 `argv` 和 `env` 都经过 `_redacted` / `_redacted_env`（[launch_plan.py:48-89](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L48-L89)）处理：名字里含 `auth_token` / `password` / `secret` / `credential` / `wandb_key` / `swanlab_key` 的环境变量会被替换成 `<redacted>`，带用户名密码的 URL 也只保留主机名。因此 `--plan` 的输出**可以安全地贴到 issue 或日志里**而不会泄露密钥。

`plan` 本身由 `build_launch_plan` 构造：

[specforge/launch_plan.py:636-648](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L636-L648) —— 它的 docstring 写得很明确：「把一份已校验的配置解析成一个**无副作用**（side-effect-free）的进程计划」。这也是 `--plan` 能做到「只看不动」的根本原因——构造 `plan` 这一步本就不产生任何进程或 GPU 操作，副作用全部集中在后续的 `run_commands` / `_train` 里。

> `plan.kind` 有四种取值：`worker`（当前进程就是 worker，直接在 cli 内执行）、`command`（单条子进程命令）、`supervisor`（监督 producer+consumer 两条命令）、`managed_supervisor`（额外托管本地 Mooncake/SGLang 服务）。本讲只关心 `--plan` 把它们统一渲染出来；四种 kind 的判定细节留到 [u3-l2](u3-l2-launch-plan.md)。

#### 4.3.4 代码实践

**实践目标**：用 `--plan` 预览一次 disaggregated run 的进程计划，并验证「覆盖会被转发进子进程命令」。

**操作步骤**：

1. 在仓库根目录（样例 YAML 里的相对路径以根目录为基准）运行：

   ```bash
   specforge train \
     --config examples/configs/qwen3-8b-eagle3-disaggregated.yaml \
     training.max_steps=10 \
     training.learning_rate=5e-5 \
     --plan
   ```

2. 阅读打印出的 JSON，重点看三处：
   - 顶层 `kind` 与 `role`（预期：这是一份在线 disaggregated 配置且非 managed_local，因此 `kind` 为 `supervisor`、`role` 为 `both`；**确切取值待本地验证**）。
   - `commands` 数组：应包含 `producer` 和 `consumer` 两条命令；每条命令的 `argv` 末尾应出现 `training.max_steps=10 training.learning_rate=5e-5`——这证明覆盖被原样转发给子进程（见 [launch_plan.py:505-519](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L505-L519) 的 `_worker_argv`）。
   - `commands[*].env` 与顶层 `worker_env`：任何疑似密钥的值应为 `<redacted>`。

3. 去掉 `--plan` 之外保持命令不变，**对比**：不加 `--plan` 时命令会真的尝试起进程（本练习**不要**真跑，除非你已备好 Mooncake/SGLang 服务）。

**需要观察的现象**：

- 命令立即返回，退出码为 0，**没有**进程被拉起、**没有** GPU 被占用、**没有** `output_dir` 被写入。
- 覆盖字符串 `training.max_steps=10` 与 `training.learning_rate=5e-5` 出现在 producer 和 consumer 两条命令的 `argv` 末尾。
- 把同样两个覆盖改成拼写错误（如 `training.max_step=10`）再跑 `--plan`：此时不再打印 JSON，而是在 `apply_overrides` 阶段抛 `ValueError`（因为路径不存在）。

**预期结果**：`--plan` 是「配置 + 拓扑」的体检入口，能让你在不消耗任何运行时资源的前提下确认覆盖是否生效、进程是否如预期拆分。

> 注意：由于这是 online disaggregated 配置，`build_launch_plan` 内部会做若干环境校验（例如 consumer 元数据库必须不存在、producer 必须有 server_urls）。在一份干净的检出上这些条件通常满足；若你本地残留了 `outputs/.../consumer-state/consumer.sqlite`，`--plan` 会因为「数据库已存在」而报错——这不是 `--plan` 的 bug，而是它忠实地复用了与真实训练相同的校验。

#### 4.3.5 小练习与答案

**练习 1**：`--plan` 会执行 `run_commands(plan)` 吗？为什么？
**答案**：不会。[cli.py:255-257](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L255-L257) 在 `if args.plan:` 分支里 `print(plan.render())` 后直接 `return 0`，控制流不会到达 [cli.py:267](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L267) 的 `run_commands(plan)`。

**练习 2**：为什么说 `--plan` 是「免费的配置体检」？
**答案**：因为它复用了真实训练的前三步——`load_config`（含覆盖与校验）、`resolve_run`（解析算法）、`build_launch_plan`（推导拓扑）。任何配置错误或非法「方法×拓扑」组合都会在这三步里暴露，而你不必为这次检查付出任何 GPU 或进程开销。

**练习 3**：`--plan` 输出里出现 `<redacted>` 是 bug 吗？
**答案**：不是。`LaunchPlan.render()` 主动用 `_redacted` / `_redacted_env`（[launch_plan.py:48-89](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch_plan.py#L48-L89)）屏蔽密钥类环境变量和带凭据的 URL，目的是让计划输出可以安全分享。被屏蔽的值在真实执行时仍是原值。

## 5. 综合实践

把本讲三个模块串成一个完整的工作流：**用覆盖 + 预览，安全地把一次大规模 run 裁成冒烟测试**。

背景：你想基于 [`qwen3-8b-eagle3-disaggregated.yaml`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-disaggregated.yaml)（原 `max_steps: 10000`）跑一次只 10 步的冒烟测试，并改用一个新的输出目录，避免污染黄金样例的 `output_dir`。

任务：

1. **写覆盖**：设计一条 `specforge train` 命令，用 `--plan` 预览，并叠加至少三个覆盖：把 `training.max_steps` 改成 `10`、把 `training.learning_rate` 改成 `5e-5`、把顶层 `output_dir` 改成你自己的目录（例如 `output_dir=./outputs/my-smoke`）。
2. **体检**：运行带 `--plan` 的命令，确认它打印 JSON 而不启动训练；从输出中找到 producer 与 consumer 两条命令，验证三条覆盖都出现在它们的 `argv` 末尾。
3. **破坏性对照**：故意把其中一条覆盖的路径写错（如 `training.max_step=10`），再跑 `--plan`，确认它在校验阶段（`apply_overrides`）就报错、不会打印任何计划。
4. **解释**：写一两句话说明，为什么即使你最终要真跑，也应该先用 `--plan` 过一遍——结合本讲学到的「两次 `model_validate`」「路径不存在即报错」「无副作用构造 plan」三点。

> 提示：第 1 步里 `output_dir` 是 `Config` 的顶层字段（[schema.py:653](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L653)），所以覆盖路径就是 `output_dir=...`，没有 section 前缀——这正好用来验证 `apply_overrides` 对「单层路径」的处理。

## 6. 本讲小结

- SpecForge 用尾部位置参数 `section.field=value` 做命令行覆盖，由 [`apply_overrides`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L892-L918) 负责把字符串写回配置字典并整体重校验。
- 覆盖路径必须**真实存在**：中间层得是 dict、末层 key 得已在字典中，否则抛 `ValueError`——这是「未知覆盖字段报错」的精确落点。
- 标量覆盖（如 `max_steps=10`）并不自己转换类型，而是借助末尾的 `Config.model_validate(raw)` 让 Pydantic 统一做强制转换与跨字段校验。
- YAML 里的未知字段由 [`StrictConfigModel`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L32-L33) 的 `extra="forbid"` 拦截；`load_config` → `from_file` → `migrate_legacy_config` → `model_validate` 是唯一加载链，旧字段只在这一个边界被翻译。
- [`--plan`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L189-L193) 复用了真实训练的「加载→解析算法→构造拓扑」前三步，却在打印 `plan.render()` 后直接返回，是零 GPU 开销的配置与拓扑体检工具。
- `plan.render()` 会脱敏密钥类环境变量与带凭据 URL，输出可安全分享；覆盖会被原样转发进 producer/consumer 子进程命令的 `argv`。

## 7. 下一步学习建议

本讲把「配置如何被改、被校验、被预览」讲透了，但故意没碰两件事：**算法是怎么按 `training.strategy` 解析出来的**，以及**进程计划 `plan.kind` 的四种取值是怎么判定的**。建议按以下顺序继续：

1. **[u3-l1 CLI 入口与命令解析](u3-l1-cli-entry.md)**：完整走一遍 `main()` → `_train()` 的调用链，理解 `--plan` 截断点之后，`plan.kind == "worker"` 与 `run_commands(plan)` 两条执行分支分别干什么。
2. **[u3-l2 启动计划 launch_plan](u3-l2-launch-plan.md)**：深入 `build_launch_plan`，弄清 `worker` / `command` / `supervisor` / `managed_supervisor` 四种 kind 的判定条件，以及 `--role`、`--node-rank` 如何影响计划。
3. **[u4-l1 算法契约 contracts](u4-l1-algorithm-contracts.md)**：本讲提到的 `resolve_run(cfg)`（[cli.py:246](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L246)）就是把 `training.strategy` 字符串变成算法注册项的入口，下一阶段进入算法注册体系时再展开。

如果想立刻动手，可以用 `--plan` 对 [`examples/configs/`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs) 下的多份样例 YAML 各跑一遍，对比 offline（`hidden_states_path`）与 online（`train_data_path`）两类配置在 `plan.kind` 与 `commands` 上的差异。
