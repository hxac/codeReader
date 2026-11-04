# 配置系统：从 JSON 到 DeepSpeedConfig

## 1. 本讲目标

学完本讲，你应该能够：

1. 写出 `train_batch_size`、`train_micro_batch_size_per_gpu`、`gradient_accumulation_steps` 三者的恒等式，并解释 DeepSpeed「三选二、缺一自动补全」的推导规则。
2. 追踪一个配置项（本讲以 `gradient_accumulation_steps` 为例）从 JSON 文件到引擎属性的完整读取链路。
3. 说明 0.19.10 周期新增的顶层布尔开关 `deterministic_mode` 如何在 `DeepSpeedConfig` 构造期经 `determinism.configure_determinism` 解析挂载，并理解「解析在 config、应用在 engine」的两阶段设计。
4. 学会用 `DeepSpeedConfigWriter` 以「模板 + 覆盖」的方式反向生成配置文件。

## 2. 前置知识

- **JSON 与 hjson**：JSON 是配置文件的事实标准，但手写时不允许注释、必须严格逗号。DeepSpeed 用 **hjson** 解析配置文件，它是 JSON 的超集——允许注释、尾逗号甚至省略引号，对人类更友好。
- **world_size 与 rank**：分布式训练中 `world_size` 是参与训练的进程总数，`rank` 是每个进程的全局编号。注意：在 DeepSpeed 配置语境下，参与 batch size 公式的不是总进程数，而是**数据并行维度上的 world_size**（流水线/张量并行的进程不算在内，本讲后面会看到代码如何处理）。
- **梯度累积（gradient accumulation）**：显存放不下大 batch 时，把一个大 batch 切成若干 micro-batch 依次前向反向，梯度先累加，攒够 `gradient_accumulation_steps` 次再做一次优化器更新。这让「有效 batch size」可以远大于单卡单步能装下的 batch。
- **pydantic**：一个用 Python 类型注解做数据校验的库。DeepSpeed 对嵌套配置块（如 `zero_optimization`）用 pydantic 模型校验，对顶层标量则用简单的 `dict.get` 读取——本讲会对比这两套机制。
- **确定性执行（deterministic execution）**：指「相同输入 + 相同种子 ⇒ 逐位（bitwise）相同的输出」。它和「数值精度」是正交的两件事：fp16 损失精度但可以确定，fp32 精度高但某些内核的并行归约顺序不定（结果每次微差）。本讲只涉及它的配置入口，深入讨论在 u8-l8。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `deepspeed/runtime/config.py` | 本讲主角：`DeepSpeedConfig` 类、几十个 `get_xxx` 读取函数、`DeepSpeedConfigWriter` |
| `deepspeed/runtime/config_utils.py` | 配置工具箱：`get_scalar_param`、pydantic 基类 `DeepSpeedConfigModel`、重复键检测 |
| `deepspeed/runtime/determinism.py` | 本周期（0.19.10）新增：`deterministic_mode` 的解析（`configure_determinism`）与应用（`apply_determinism`） |
| `deepspeed/runtime/constants.py` | 所有配置键名字符串与默认值（三个 batch size 键的默认值都是 `None`，这是「三选二」得以实现的关键） |
| `deepspeed/runtime/engine.py` | 消费者视角：引擎构造早期应用确定性策略、运行时动态改写 batch size 的两个方法 |
| `docs/_pages/config-json.md` | 官方配置文档，本周期新增 Deterministic Execution 一节 |

## 4. 核心概念与源码讲解

### 4.1 DeepSpeedConfig 构造：一份配置的三种传入形态与读取链路

#### 4.1.1 概念说明

`initialize()` 接收的 `config` 参数（见 u2-l1）最终会被翻译成一个 `DeepSpeedConfig` 对象。这个类是「用户写的声明式 JSON」与「引擎需要的命令式 Python 属性」之间的翻译层：

- 用户侧希望配置**好写好读**：hjson、可以省略字段、缺的字段自动推导；
- 引擎侧希望配置**好用**：`engine.gradient_accumulation_steps` 直接是 int，`engine.zero_optimization_stage` 直接是数字，不用到处 `dict.get` 加默认值。

`DeepSpeedConfig` 在构造函数里一次性完成：读取 → 拒绝非法键 → 推导 batch size → 解析确定性开关 → 一致性检查。构造完成后，这份配置对象就从「文本」变成了「强类型的引擎说明书」。

#### 4.1.2 核心流程

```text
deepspeed.initialize(model, config=...)
    └─ config_class = DeepSpeedConfig(config, mpu, mesh_device)      # deepspeed/__init__.py
           └─ DeepSpeedConfig.__init__
                1. 解析 config 的三种形态 → self._param_dict（原始字典）
                2. _reject_removed_config_keys   # 已删除特性直接报错（fail-fast）
                3. 探测 global_rank / world_size # 未初始化分布式时回退 rank=0, world=1
                4. param_dict = copy.copy(...)   # 之后所有解析都作用于副本，原始 JSON 不被污染
                5. _initialize_params(param_dict)        # 把几十个字段挂成 self.xxx 属性
                6. _configure_train_batch_size()         # batch size 三选二补全 + 断言
                7. configure_determinism(self, param_dict)  # 本周期新增：解析 deterministic_mode
                8. _do_sanity_check()            # 错误检查 + 警告检查
```

#### 4.1.3 源码精读

**入口：initialize() 里在哪里构造它。** 非 PipelineModule 模型走这一条，构造结果作为 `config_class` 传给 `DeepSpeedEngine`：

- [deepspeed/__init__.py:553-554](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/__init__.py#L553-L554) — 普通模型分支中 `config_class = DeepSpeedConfig(config, mpu, mesh_device=mesh_device)`；流水线模型在 [deepspeed/__init__.py:584](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/__init__.py#L584) 走同一构造（不带 mesh_device）。

**第一步：三种传入形态。** `config` 参数可以是 dict、文件路径或 base64 字符串：

- [deepspeed/runtime/config.py:441-454](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L441-L454) — 依次判断：dict 直接用；存在的文件路径用 `hjson.load`（注意 `object_pairs_hook=dict_raise_error_on_duplicate_keys`）；否则尝试 base64 解码，全部失败才抛 `ValueError`。

三种形态各有用途：dict 方便在 Python 代码里动态拼装；文件路径是日常用法；base64 则服务于「把配置塞进命令行/环境变量传给别的进程」的场景（回忆 u1-l4 讲过的启动器资源编码，思路相同）。

**重复键直接报错。** hjson 放宽了语法，但重复键是手写配置的高发事故（两处写了同一个键，后者悄悄覆盖前者），所以解析时主动拒绝：

- [deepspeed/runtime/config_utils.py:205-212](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config_utils.py#L205-L212) — `dict_raise_error_on_duplicate_keys`：把有序键值对收进 dict，数量变短即说明有重复，抛出 `ValueError` 并点名重复的键。

**第二步：拒绝已移除的配置键。** DeepSpeed 清理过一批历史特性（Nebula 检查点、Apex AMP、稀疏注意力等）。如果用户拿着旧配置文件来跑，静默忽略会让行为与预期不符（例如以为在用 AMP 其实没有），所以直接报错并给出迁移指引：

- [deepspeed/runtime/config.py:456](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L456) — 构造早期调用 `_reject_removed_config_keys`。
- [deepspeed/runtime/config.py:133-141](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L133-L141) — 遍历 `_REMOVED_TOP_LEVEL_CONFIG_KEYS`（定义在 [config.py:81-118](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L81-L118)）和 `zero_optimization` 子块里的已移除键，命中即抛 `DeepSpeedConfigError`，错误信息里带 issue 链接和替代写法。

**第三步：确定 world_size。** batch size 公式需要数据并行宽度，它不是配置项而是运行时拓扑：

- [deepspeed/runtime/config.py:458-477](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L458-L477) — 优先用 `mpu`（用户自定义的 model-parallel unit，Ulysses 序列并行时除以 `sequence_parallel_world_size`），其次 `mesh_device`，再次裸 `dist.get_world_size()`；异常时（[L474-L476](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L474-L476)）回退 `global_rank=0, world_size=1`。

这个回退很重要：它让 `DeepSpeedConfig` 在**单进程、未初始化分布式**的环境（比如你的笔记本、CI 单测）里也能构造，本讲的实践正是利用这一点。

**第四~八步：四步流水线。**

- [deepspeed/runtime/config.py:478-484](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L478-L484) — 先 `copy.copy` 出副本（注释说明是为了让用户 JSON 原样保留用于日志），然后依次执行 `_initialize_params` → `_configure_train_batch_size` → `configure_determinism`（本周期新增的两行，4.3 节详述）→ `_do_sanity_check`。

**`_initialize_params`：一次挂载几十个属性。** 这是「翻译层」的主体：

- [deepspeed/runtime/config.py:486-592](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L486-L592) — `_initialize_params` 把 `train_batch_size`、`steps_per_print`、通信精度、ZeRO、监控、调度器等字段逐个变成 `self.xxx`。几个代表性片段：
  - [L503-L505](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L503-L505)：`zero_config = get_zero_config(param_dict)` 之后立刻派生 `zero_optimization_stage` 和 `zero_enabled`——嵌套块解析一次，顶层视角同步生成。
  - [L513-L516](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L513-L516)：fp16 与 bf16 同时启用直接断言失败——两个互斥的精度模式不能共存，这类「跨块一致性」就放在这里检查。

**两套读取机制。** 纵观 `_initialize_params`，配置值进入引擎有两条路：

1. **扁平标量：`get_scalar_param`**，即 `dict.get(name, default)` 一行封装：
   - [deepspeed/runtime/config_utils.py:193-194](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config_utils.py#L193-L194) — 不做任何类型校验，靠后续 `_do_sanity_check` 和使用点兜底。键名和默认值统一定义在 constants.py（例如 [deepspeed/runtime/constants.py:72](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/constants.py#L72) 的 `GRADIENT_ACCUMULATION_STEPS_DEFAULT = None`）。
2. **嵌套块：pydantic 模型 `DeepSpeedConfigModel`**：
   - [deepspeed/runtime/config_utils.py:17-58](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config_utils.py#L17-L58) — 继承 `pydantic.BaseModel`，支持「弃用字段自动迁移到新字段」；[L54-L57](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config_utils.py#L54-L57) 还会把值等于 `"auto"` 的字段静默丢弃（`replace_method` 除外）——这让带 `"auto"` 占位符的 HuggingFace 配置能顺利加载，缺省值由 pydantic 默认值接管。
   - [deepspeed/runtime/config_utils.py:105-113](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config_utils.py#L105-L113) — `model_config` 里 `extra="forbid"`：嵌套块里写错字段名会直接报错，而不是被无声忽略。

**读取链路闭环：引擎如何消费。** 以 `gradient_accumulation_steps` 为例，完整链路是：

```text
JSON "gradient_accumulation_steps": 4
  → constants.GRADIENT_ACCUMULATION_STEPS（键名）+ GRADIENT_ACCUMULATION_STEPS_DEFAULT（默认 None）
  → config.get_gradient_accumulation_steps(param_dict)      # config.py:208-209，经 get_scalar_param
  → DeepSpeedConfig.gradient_accumulation_steps             # _initialize_params 挂载
  → DeepSpeedEngine.gradient_accumulation_steps             # engine.py:1716-1717 的 @property 直接转发
```

- [deepspeed/runtime/engine.py:1716-1717](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/engine.py#L1716-L1717) — 引擎上的同名 property 只是 `return self._config.gradient_accumulation_steps`，说明引擎把 `DeepSpeedConfig` 当作**唯一事实来源**（single source of truth），自身不另存一份。

#### 4.1.4 代码实践

**实践目标**：验证 `DeepSpeedConfig` 在单进程下可独立构造，并观察它输出的解析结果。

**操作步骤**：

1. 准备一个最小配置文件 `mini.json`（示例代码）：

```json
{
    "train_micro_batch_size_per_gpu": 4,
    "gradient_accumulation_steps": 2,
    // hjson 允许注释
    "steps_per_print": 1
}
```

2. 运行如下脚本（示例代码）：

```python
import deepspeed
from deepspeed.runtime.config import DeepSpeedConfig

cfg = DeepSpeedConfig("mini.json")
print("batch:", cfg.train_batch_size)                # 期望 8 = 4 * 2 * world_size(=1)
print("micro :", cfg.train_micro_batch_size_per_gpu) # 4
print("gas   :", cfg.gradient_accumulation_steps)    # 2
cfg.print("DeepSpeedConfig")  # 打印全部已解析属性与原始 JSON
```

3. 再故意在 `mini.json` 里写两个 `"steps_per_print"`，重新运行。

**需要观察的现象**：第 2 步日志里 `world_size = 1`（未初始化分布式时的回退），且 `train_batch_size` 被自动补全为 8；第 3 步抛出 `ValueError: Duplicate keys in DeepSpeed config: ['steps_per_print']`。

**预期结果**：配置对象脱离引擎与 GPU 也能完成解析，这正是 4.3 节「解析与应用分离」的前提。逐步输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DeepSpeedConfig.__init__` 里对 `param_dict` 做 `copy.copy`，而不是直接用 `self._param_dict`？

**答案**：[config.py:478-479](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L478-L479) 的注释写明是为了「让用户 JSON 保持原样（例如用于日志打印）」。后续解析阶段可能对字典做增删（pydantic 弃用字段迁移、`"auto"` 过滤），作用在副本上可以保证 `print_user_config`（[config.py:667-675](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L667-L675)）展示的永远是用户写的原始内容。

**练习 2**：把 `"amp": {...}` 加进配置会发生什么？为什么要这样设计而不是忽略它？

**答案**：`_reject_removed_config_keys`（[config.py:133-141](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L133-L141)）命中 `_REMOVED_TOP_LEVEL_CONFIG_KEYS` 里的 `amp`，抛 `DeepSpeedConfigError`，信息提示改用 `fp16`/`bf16`/`torch_autocast`。静默忽略会让用户以为在用 AMP 训练、实际却以 fp32 裸跑——数值行为完全不同却没有任何征兆，所以必须 fail-fast。

**练习 3**：`get_scalar_param` 与 pydantic 的 `DeepSpeedConfigModel` 各自适合什么字段？

**答案**：顶层简单标量（batch size、`steps_per_print`、`deterministic_mode`）用 `get_scalar_param`——结构扁平、类型一眼可见，校验交给 sanity check；嵌套结构块（`zero_optimization`、`hybrid_engine`）用 pydantic——字段多、层级深，需要 `extra="forbid"` 拦截拼错键名、需要类型校验和弃用字段迁移。

### 4.2 batch size 推导公式：三选二自动补全

#### 4.2.1 概念说明

三个 batch size 参数被一条恒等式绑定：

\[ \text{train\_batch\_size} = \text{train\_micro\_batch\_size\_per\_gpu} \times \text{gradient\_accumulation\_steps} \times \text{world\_size} \]

- `train_batch_size`：**有效**训练 batch——多少个样本对应一次模型更新；
- `train_micro_batch_size_per_gpu`：单卡一次前向/反向吃下的样本数（受显存约束）；
- `gradient_accumulation_steps`：攒多少个 micro-batch 才做一次优化器 step；
- `world_size`：数据并行宽度（注意不是总进程数）。

手写三个数容易自相矛盾（比如想当然写 `gas=1`），所以 DeepSpeed 允许**只写其中两个，第三个按公式解出来**，并且最后一定断言恒等式成立。官方文档在 [docs/_pages/config-json.md:21-43](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/docs/_pages/config-json.md#L21-L43) 明确写了这条规则。

#### 4.2.2 核心流程

`_set_batch_related_parameters` 是一张七分支决策表（`None` 表示用户没写）：

| train_batch | micro | gas | 行为 |
| --- | --- | --- | --- |
| ✓ | ✓ | ✓ | 全给了，直接返回 |
| ✓ | ✓ | ✗ | \( \text{gas} = \text{train\_batch} \div \text{micro} \div \text{world\_size} \) |
| ✓ | ✗ | ✓ | \( \text{micro} = \text{train\_batch} \div \text{world\_size} \div \text{gas} \) |
| ✗ | ✓ | ✓ | \( \text{train\_batch} = \text{micro} \times \text{gas} \times \text{world\_size} \) |
| ✓ | ✗ | ✗ | gas=1，\( \text{micro} = \text{train\_batch} \div \text{world\_size} \) |
| ✗ | ✓ | ✗ | gas=1，\( \text{train\_batch} = \text{micro} \times \text{world\_size} \) |
| ✗ | ✗ | （任意） | 断言失败：至少要给 train_batch_size 或 micro 之一 |

推导完成后 `_batch_assertion` 再按恒等式整体校验一遍（防止除法取整后对不上）。

#### 4.2.3 源码精读

**默认值全是 `None`，这是「三选二」的基石。** 如果默认值是 1，就无法区分「用户显式写了 1」和「用户没写」：

- [deepspeed/runtime/constants.py:20](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/constants.py#L20)、[constants.py:60](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/constants.py#L60)、[constants.py:72](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/constants.py#L72) — 三个键的 `*_DEFAULT` 均为 `None`。

**读取函数只是薄封装**：

- [deepspeed/runtime/config.py:339-348](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L339-L348) — `get_train_batch_size` / `get_train_micro_batch_size_per_gpu`。
- [deepspeed/runtime/config.py:208-209](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L208-L209) — `get_gradient_accumulation_steps`。

**七分支决策表**：

- [deepspeed/runtime/config.py:611-656](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L611-L656) — `_set_batch_related_parameters`，与上表逐行对应。注意 gas 推导分支（[L624-L627](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L624-L627)）用的是**整除** `//`——如果 `train_batch` 不能被整除，结果会悄悄变小，随后由断言兜底报错。

**恒等式断言**：

- [deepspeed/runtime/config.py:594-609](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L594-L609) — `_batch_assertion`：三个值必须为正，且 `train_batch == micro * grad_acc * self.world_size`，不满足时把三个数的实际值打进错误消息。

**装配点**：

- [deepspeed/runtime/config.py:658-660](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L658-L660) — `_configure_train_batch_size` = 先补全、后断言。

**公式在运行期依然被遵守。** 引擎提供动态调整 batch 的方法，改一个量时按恒等式联动改写 `self._config` 上的另两个量：

- [deepspeed/runtime/engine.py:1331-1349](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/engine.py#L1331-L1349) — `set_train_batch_size`：micro 不变，\( \text{new\_gas} = \text{train\_batch} \div (\text{micro} \times \text{dp\_world\_size}) \)，然后**直接覆写** `self._config.train_batch_size / gradient_accumulation_steps`（配置对象是活的，不是构造完就冻结）。
- [deepspeed/runtime/engine.py:1351-1359](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/engine.py#L1351-L1359) — `set_train_micro_batch_size`：gas 不变，按 \( \text{micro} \times \text{gas} \times \text{dp\_world\_size} \) 重算全局 batch。

#### 4.2.4 代码实践

**实践目标**：用三个只给两个参数的配置验证推导规则与手算一致。

**操作步骤**：

1. 写三个配置文件（示例代码）：

```json
// a.json：给 batch + micro，缺 gas
{ "train_batch_size": 32, "train_micro_batch_size_per_gpu": 8 }
// b.json：给 batch + gas，缺 micro
{ "train_batch_size": 32, "gradient_accumulation_steps": 4 }
// c.json：给 micro + gas，缺 batch
{ "train_micro_batch_size_per_gpu": 8, "gradient_accumulation_steps": 4 }
```

2. 单进程运行（示例代码）：

```python
from deepspeed.runtime.config import DeepSpeedConfig
for f in ["a.json", "b.json", "c.json"]:
    c = DeepSpeedConfig(f)
    print(f, c.train_batch_size, c.train_micro_batch_size_per_gpu, c.gradient_accumulation_steps)
```

3. 再写一个 `train_batch_size: 30, train_micro_batch_size_per_gpu: 8` 的配置（30 不能被 8 整除）。

**需要观察的现象**：单进程下 `world_size=1`，三个文件都应解析成 `(32, 8, 4)`；第 3 步应触发 `_batch_assertion` 的断言错误（30 // 8 // 1 = 3，3×8×1=24≠30）。

**预期结果**：与手算完全一致；整除失败的场景报错信息里能看到三个实际值。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：8 卡训练，想要有效 batch 512、单卡 micro batch 16，gas 该写多少？如果干脆不写 gas 呢？

**答案**：\( 512 = 16 \times \text{gas} \times 8 \Rightarrow \text{gas} = 4 \)。不写也可以：DeepSpeed 走「train_batch + micro 已给」分支，`grad_acc = 512 // 16 // 8 = 4`（[config.py:624-627](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L624-L627)）。

**练习 2**：为什么 batch size 公式里的 `world_size` 要从 `mpu`/`mesh_device` 推导，而不能直接用 `dist.get_world_size()`？

**答案**：[config.py:458-477](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L458-L477)。公式需要的是**数据并行**宽度。开了张量/流水线/序列并行时，总进程数包含非数据维度的进程，直接用总进程数会错估 batch。所以优先从 `mpu.get_data_parallel_world_size()`（或 mesh 的 data_parallel 组、或总 world_size 除以 sequence_parallel_size）拿数据并行宽度。

### 4.3 configure_determinism 挂载点：deterministic_mode 的解析与应用（本周期新增）

#### 4.3.1 概念说明

0.19.10 周期（提交 dfc63adfb）新增了顶层布尔开关 `deterministic_mode`：opt-in 的确定性执行策略。它解决的问题是「两次相同种子、相同数据的训练，结果是否逐位一致」——调试 divergence、复现实验、对拍两个实现时这是刚需。

设计上它分成**两个阶段**：

1. **解析阶段（配置构造期）**：`DeepSpeedConfig.__init__` 里调用 `configure_determinism`，只做「读 JSON + 严格类型检查 + 把布尔值挂到 config.deterministic_mode 上」。不碰 torch、不碰硬件，所以在任何机器（包括纯 CPU）上构造配置都不会失败。
2. **应用阶段（引擎构造期）**：`DeepSpeedEngine.__init__` 早期调用 `apply_determinism`，此时才检查加速器类型、环境变量，并真正翻转 torch 的确定性开关。

为什么分离？因为 `DeepSpeedConfig` 的使命是「忠实地描述配置」（可以在任何地方构造、打印、序列化），而「这个进程允不允许非确定算法」是引擎接管进程时才该回答的问题。解析期失败 = 配置写错了；应用期失败 = 环境不满足。

#### 4.3.2 核心流程

```text
JSON "deterministic_mode": true
  ├─ 解析（DeepSpeedConfig.__init__，本模块焦点）
  │    configure_determinism(config, param_dict)
  │      1. param_dict.get("deterministic_mode", False)
  │      2. type(v) is not bool → ValueError   # 1/0/"true" 一律拒绝
  │      3. config.deterministic_mode = v
  └─ 应用（DeepSpeedEngine.__init__ 早期，详见 u2-l3 / u8-l8）
       apply_determinism(config)
         1. 开关未开 → 直接返回
         2. 非 NVIDIA CUDA → ValueError（KLXPU/ROCm 也算非）
         3. CUBLAS_WORKSPACE_CONFIG 不合法 → ValueError
         4. torch.use_deterministic_algorithms(True, warn_only=False)
         5. 关 TF32（matmul + cudnn）、关 cudnn.benchmark、开 cudnn.deterministic
```

#### 4.3.3 源码精读

**挂载点：插在 batch size 之后、sanity check 之前。**

- [deepspeed/runtime/config.py:481-484](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L481-L484) — `_configure_train_batch_size()` 之后两行 `from .determinism import configure_determinism; configure_determinism(self, param_dict)`，然后才是 `_do_sanity_check()`。函数内 import 而非顶部 import，是 DeepSpeed 惯用的按需加载（避免拖慢 `import deepspeed`）。位置选择有讲究：开关必须在 `_do_sanity_check` 之前挂好，这样后续任何基于 `config.deterministic_mode` 的一致性检查（如与 `stochastic_mode` 互斥的校验，见 u9-l2）都能读到它。

**解析函数：严格布尔。**

- [deepspeed/runtime/determinism.py:10-14](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/determinism.py#L10-L14) — `configure_determinism`。注意判断写的是 `type(enabled) is not bool` 而非 `isinstance`：Python 里 `isinstance(True, int)` 为真、`isinstance(1, bool)` 为假，JSON 的 `1` 会被解析成 int——用 `type() is bool` 才能把 `"deterministic_mode": 1` 这种似是而非的写法拒之门外，避免「用户以为开了其实没开」或反之。

**应用点：引擎构造的最早期。**

- [deepspeed/runtime/engine.py:543-547](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/engine.py#L543-L547) — `self._config = config_class` 紧接着就是 `if self._config.deterministic_mode: apply_determinism(self._config)`。越早应用越好：任何后续内核（包括 AutoTP 注入、优化器包装）都必须落在确定性开关生效之后的进程状态里。

**应用函数的三道闸门与五个开关。**

- [deepspeed/runtime/determinism.py:24-38](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/determinism.py#L24-L38) — `apply_determinism`：
  - **闸门 1（硬件）**：`_is_nvidia_cuda` 不通过就抛 `ValueError`，当前只支持 NVIDIA CUDA。
  - **闸门 2（环境变量）**：`CUBLAS_WORKSPACE_CONFIG` 必须是 `:4096:8` 或 `:16:8`。源码注释（[L31](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/determinism.py#L31)）点破了为什么**引擎不代劳设置**：引擎构造时 CUDA 可能已经初始化过 cuBLAS workspace，此时改环境变量为时已晚——与其假装生效，不如拒绝启动让用户在启动脚本里提前设好。
  - **五个 torch 开关**：`use_deterministic_algorithms(True, warn_only=False)`（不确定的 op 直接抛错而非警告）、matmul 与 cudnn 的 TF32 都关掉（TF32 用 tensor core 做低精度矩阵乘，归约顺序不定）、`cudnn.benchmark=False` + `cudnn.deterministic=True`（禁用自动算法试探，选确定性卷积算法）。
- [deepspeed/runtime/determinism.py:17-21](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/determinism.py#L17-L21) — `_is_nvidia_cuda` 的三重判断值得细读：KLXPU 后端**继承 CUDA 的 `device_name()`**（返回 `"cuda"`）但 `_name` 是 `"klxpu"`；ROCm 同样报 `"cuda"` 但 `torch.version.hip` 非空。所以必须同时核对 `device_name()`、`_name`、`torch.version.hip` 三处才能确认「真 NVIDIA CUDA」——这是 u3-l1 加速器抽象层多后端并存的直接代价。

**官方文档声明的边界。**

- [docs/_pages/config-json.md:7-19](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/docs/_pages/config-json.md#L7-L19) — 文档说得很克制：该开关**不改变**精度、NCCL 算法、并行度与重叠；PyTorch 的确定性检查覆盖不了所有自定义内核与分布式调度，「配置被接受」不等于「工作负载逐位可复现」；且设置是进程级的、teardown 时不会恢复。

#### 4.3.4 代码实践

**实践目标**：亲眼区分「解析阶段」（哪里都能跑）与「应用阶段」（环境不满足就报错）。

**操作步骤**：

1. 准备配置 `det.json`（示例代码）：

```json
{ "train_micro_batch_size_per_gpu": 4, "gradient_accumulation_steps": 2, "deterministic_mode": true }
```

2. 单进程运行（示例代码）：

```python
from deepspeed.runtime.config import DeepSpeedConfig
from deepspeed.runtime.determinism import apply_determinism

cfg = DeepSpeedConfig("det.json")
print("parsed:", cfg.deterministic_mode)   # True：解析阶段完成，机器上没有 GPU 也走到这里
apply_determinism(cfg)                     # 手动触发应用阶段
```

3. 在**无 NVIDIA GPU** 的机器（或 CPU 后端）上观察第 2 步的报错。
4. 若在有 GPU 的机器上：先不设环境变量运行（观察 CUBLAS 报错），再以 `CUBLAS_WORKSPACE_CONFIG=:4096:8 python xxx.py` 重新运行（应能通过闸门）。
5. 把 JSON 里的 `true` 改成 `1`，重新构造 `DeepSpeedConfig`。

**需要观察的现象**：第 2 步打印 `True` 证明解析无硬件依赖；第 3 步抛 `ValueError: deterministic_mode currently supports NVIDIA CUDA only`；第 4 步 GPU 上先抛 CUBLAS 相关 `ValueError`、设好环境变量后通过；第 5 步抛 `ValueError: deterministic_mode must be a JSON boolean`。

**预期结果**：解析/应用两阶段的失败模式截然不同且都可复现。本实践在 CPU 机器上的部分可确定执行，GPU 分支待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `apply_determinism` 不自己 `os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"`，反而宁可报错？

**答案**：[determinism.py:31-33](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/determinism.py#L31-L33)。cuBLAS 的 workspace 布局在 CUDA 初始化时就定了，引擎构造时很可能已经晚了——改了环境变量也不会生效，形成「看起来开了确定性、实际没有」的静默陷阱。报错把责任交还给用户：在启动脚本里、CUDA 使用之前设好。

**练习 2**：`deterministic_mode` 与 fp16/bf16 是什么关系？开了它会不会让训练「更精确」？

**答案**：没有关系。文档（[config-json.md:15-18](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/docs/_pages/config-json.md#L15-L18)）明确它**不改变精度**——它管的是「相同输入是否得到逐位相同输出」，不是「输出更准」。确定性路径甚至常常更慢（关 TF32、禁 cudnn benchmark）。determinism.py 的模块 docstring（[L3](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/determinism.py#L3)）也强调它「independent of numerical compatibility」。

**练习 3**：`configure_determinism` 为什么放在 `_do_sanity_check()` 之前调用？

**答案**：[config.py:481-484](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L481-L484)。sanity check 及后续所有消费者（包括引擎侧与其他配置块里针对 `deterministic_mode` 的一致性校验）需要读到已挂载的 `config.deterministic_mode` 属性；先挂载再检查，检查才能覆盖它。

### 4.4 DeepSpeedConfigWriter：配置的反向生成

#### 4.4.1 概念说明

`DeepSpeedConfig` 是「JSON → 对象」，`DeepSpeedConfigWriter` 是「对象/字典 → JSON」。典型用法是**模板改写**：加载一份基准配置，改几个键，写出新文件。自动化流程（如自动调优尝试不同超参组合、脚本批量生成实验配置）需要这种能力，手工编辑几十份 JSON 既容易错也难以批量。

#### 4.4.2 核心流程

```text
writer = DeepSpeedConfigWriter()           # 空字典起步
writer.load_config("base.json")            # 或从模板载入
writer.add_config("train_batch_size", 64)  # 覆盖/新增一个键
writer.add_config("bf16", {"enabled": True})
writer.write_config("new.json")            # 序列化落盘
```

#### 4.4.3 源码精读

- [deepspeed/runtime/config.py:419-436](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L419-L436) — `DeepSpeedConfigWriter` 全部实现只有三个方法：`add_config`（字典赋值）、`load_config`（`json.load`，同样挂了重复键检测 `dict_raise_error_on_duplicate_keys`，读模板时就拦截坏文件）、`write_config`（`json.dump` 落盘）。类上方的注释（[L419-L420](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L419-L420)）写明其意图：通过修改基础模板来写配置文件，服务于「命令行参数快速改配置」的场景。

需要诚实说明：当前仓库内检索不到它的其他调用点（仅此定义处），应视为**暴露给用户与外部脚本使用的公共工具**，而非内部热路径。它写出的是标准 JSON（不是 hjson），再次加载时两条路都兼容。

#### 4.4.4 代码实践

**实践目标**：用「模板 + 覆盖」批量生成一组只在 batch size 上不同的配置，并确认生成物可被 `DeepSpeedConfig` 正确加载。

**操作步骤**：

1. 运行（示例代码）：

```python
from deepspeed.runtime.config import DeepSpeedConfigWriter

writer = DeepSpeedConfigWriter()
writer.load_config("mini.json")            # 复用 4.1.4 的模板
for batch in [16, 32, 64]:
    writer.add_config("train_batch_size", batch)
    writer.add_config("train_micro_batch_size_per_gpu", batch // 4)
    writer.write_config(f"cfg_{batch}.json")
    check = DeepSpeedConfig(f"cfg_{batch}.json")   # 立即回读验证
    assert check.train_batch_size == batch and check.gradient_accumulation_steps == 4
```

2. 打开任一 `cfg_*.json` 查看落盘内容。

**需要观察的现象**：生成三个文件；每个回读后 `gradient_accumulation_steps` 都被自动补全为 4（因为 micro=batch/4、world_size=1）。

**预期结果**：写入的 JSON 与手工书写的效果完全等价，`DeepSpeedConfig` 的解析对两者一视同仁。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`load_config` 为什么也要用 `dict_raise_error_on_duplicate_keys`？

**答案**：[config.py:431-432](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/config.py#L431-L432)。模板本身可能带重复键（手改文件时常见）。若在读取时不报错，`json.load` 默认行为是后者覆盖前者，之后基于污染数据生成的所有派生配置都会继承这个错误。在入口处用与 `DeepSpeedConfig` 相同的标准拦截，保证「读进来的模板」是干净的。

**练习 2**：想给自动调优实验批量生成「ZeRO stage 1/2/3 × 是否 CPU offload」共 6 份配置，用本讲的工具怎么组织代码？

**答案**：双重循环 + `DeepSpeedConfigWriter`：外层 `for stage in [1, 2, 3]`，内层 `for offload in [False, True]`，每轮 `load_config` 同一份基础模板，`add_config("zero_optimization", {...})` 写入当前组合（offload 为真时加 `offload_optimizer: {"device": "cpu"}`），`write_config` 按组合命名落盘。每份生成后立即用 `DeepSpeedConfig` 回读断言 `zero_optimization_stage` 与预期一致，尽早发现模板被上一轮污染等问题。

## 5. 综合实践

把本讲四个最小模块串起来：手写一份「bf16 + ZeRO-2 + CPU offload + deterministic_mode」的完整配置，加载并断言所有推导值，再体验确定性开关的两阶段行为，最后用 Writer 派生一份变体。

**步骤**：

1. 手写 `ds_train.json`（示例代码）：

```json
{
    "train_batch_size": 32,
    "train_micro_batch_size_per_gpu": 8,
    "steps_per_print": 1,
    "bf16": { "enabled": true },
    "zero_optimization": {
        "stage": 2,
        "offload_optimizer": { "device": "cpu", "pin_memory": true }
    },
    "deterministic_mode": true
}
```

2. 单进程运行验证脚本（示例代码）：

```python
from deepspeed.runtime.config import DeepSpeedConfig
from deepspeed.runtime.config_utils import get_scalar_param

cfg = DeepSpeedConfig("ds_train.json")

# ① batch size 三选二：gas = 32 // 8 // 1 = 4（world_size 回退为 1）
assert cfg.gradient_accumulation_steps == 32 // 8 // 1 == 4
assert cfg.train_batch_size == 32 and cfg.train_micro_batch_size_per_gpu == 8

# ② 读取链路终点：嵌套块已变成强类型对象
assert cfg.bfloat16_config.enabled is True
assert cfg.zero_optimization_stage == 2 and cfg.zero_enabled is True

# ③ 本周期新增的顶层布尔开关已在配置构造期挂载（仅解析，未应用）
assert cfg.deterministic_mode is True

# ④ 顶层标量走 get_scalar_param 通道，键名/默认值来自 constants.py
assert get_scalar_param({"a": 7}, "a", None) == 7

cfg.print("验证通过的配置")   # 日志里对照全部属性
```

3. （可选，NVIDIA CUDA 机器）先 `export CUBLAS_WORKSPACE_CONFIG=:4096:8`，把这份配置接进 u2-l1 的 SimpleModel 训练脚本，跑两次相同种子的训练对比逐步 loss；不设环境变量再跑一次，观察引擎构造期的报错。
4. 用 `DeepSpeedConfigWriter` 载入 `ds_train.json`，把 `deterministic_mode` 改为 `false`、`train_batch_size` 改为 16，写出 `ds_train_nodet.json` 并重新加载，断言 `deterministic_mode is False` 且 `gradient_accumulation_steps == 2`。

**预期结果**：单进程下 ①②③④ 全部断言通过；步骤 3 在 GPU 上两次训练 loss 一致、缺环境变量时启动即报 `ValueError`；步骤 4 派生配置的推导值随 batch 联动变化。除 ①-④ 的推导逻辑可直接从 4.2 的源码推出外，运行现象待本地验证。

## 6. 本讲小结

- `DeepSpeedConfig` 是 JSON 与引擎之间的翻译层：支持 dict / hjson 文件 / base64 三种输入，构造期完成「解析 → 拒绝已移除键与重复键 → 推导 batch size → 挂载 deterministic_mode → sanity check」五件事，且全程作用于用户 JSON 的副本。
- batch size 三参数被恒等式 \( \text{train\_batch} = \text{micro} \times \text{gas} \times \text{world\_size} \) 绑定，`_set_batch_related_parameters` 按七分支决策表三选二自动补全，默认值全为 `None` 是补全机制的基石；引擎运行期的 `set_train_batch_size` 等方法也遵守同一公式直接改写配置对象。
- 配置值进入引擎有两条路：顶层标量走 `get_scalar_param`（无校验的 `dict.get`），嵌套块走 pydantic 的 `DeepSpeedConfigModel`（`extra="forbid"` + 弃用字段迁移 + `"auto"` 过滤）。
- 本周期新增的 `deterministic_mode` 采用两阶段设计：`configure_determinism` 在配置构造期做严格布尔解析（挂在 batch size 推导之后、sanity check 之前），`apply_determinism` 在引擎构造最早期应用（仅支持 NVIDIA CUDA、要求预先设好 `CUBLAS_WORKSPACE_CONFIG`，然后翻转 torch 的五个确定性开关）。
- `DeepSpeedConfigWriter` 提供模板改写式的反向生成能力，适合批量产生实验配置；当前仓库内无其他调用点，属面向用户的公共工具。

## 7. 下一步学习建议

配置对象在 `DeepSpeedEngine.__init__` 里被逐段消费——下一讲 **u2-l3（引擎构造：DeepSpeedEngine.__init__ 全流程）** 将按时间顺序走完引擎构造的每个阶段，其中就包括本讲埋下的伏笔：`apply_determinism` 在引擎最早期的应用点，以及 AutoEP/AutoTP 注入、优化器包装等如何围绕 `self._config` 展开。想提前了解确定性开关的完整语义（Triton 内核的 IEEE 路径、与 `stochastic_mode` 的互斥），可先读 [deepspeed/runtime/determinism.py](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/deepspeed/runtime/determinism.py) 与 [docs/_pages/config-json.md:7-19](https://github.com/deepspeedai/DeepSpeed/blob/d8241a9ba266405e1a58fadf19e24b2370f6d7fa/docs/_pages/config-json.md#L7-L19)，完整机制留到 u8-l8。
