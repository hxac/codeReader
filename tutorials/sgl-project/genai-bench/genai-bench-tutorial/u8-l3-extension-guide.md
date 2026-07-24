# 扩展指南：添加新后端 / 任务 / 场景

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 genai-bench 里**三个可扩展点**——场景（Scenario）、采样器（Sampler）、后端用户（User）——分别扩展什么，以及它们靠什么机制被系统「发现」。
- 理解「注册表 + `__init_subclass__` 自动注册」这一统一心智模型，并能判断：新增某样东西时，到底是「定义即生效」还是「需要手动登记」。
- 仿照 `OpenAIUser` 写出一个极简的 `CustomUser` 子类，并准确列出让它被 `--api-backend` 选中所需的**全部注册点**。
- 阅读官方 `docs/developer-guide/adding-new-features.md` 时，能把里面的步骤一一对应到真实源码。

本讲是「专家层」的扩展开发讲，承接 u3-l3（多后端 User 体系）、u2-l4（采样器）、u2-l2（场景定义），把三者放到「二次开发」的视角下重新串起来。

## 2. 前置知识

在动手扩展之前，请确认你已经理解下面这些前置概念（均来自依赖讲义）：

- **任务字符串 `<input>-to-<output>`**（u2-l1）：它是贯穿全库的总开关，输入模态决定采样器，输出模态决定请求类型。
- **场景（Scenario）**（u2-l2）：用微型语言 `N(...)/(...)`、`D(...)`、`I(...)` 等描述每个请求的输入/输出规模，由 `Scenario.from_string` 解析。
- **采样器（Sampler）**（u2-l4）：把「场景」与「数据集」揉成可发送的 `UserRequest`，由 `Sampler.create(task)` 工厂按输入模态分发。
- **User 后端**（u3-l1 / u3-l3）：每个厂商一个 `BaseUser` 子类，用 `supported_tasks` 字典声明能力，靠 `API_BACKEND_USER_MAP` 注册表被 CLI 选中。

本讲要用到的两个 Python 机制，先通俗解释：

- **`__init_subclass__`**：当一个类被定义、并且有别的类继承它时，父类的 `__init_subclass__(cls)` 会被自动调用，参数 `cls` 指向**新定义的子类**。genai-bench 借此在「子类诞生那一刻」把它塞进一张注册表——所以你只要写好子类，它就被登记了，无需任何额外调用。
- **注册表（registry）**：本质就是一张 `Dict[str, Type]` 字典，键是「类型标记」（如模态名、场景字母），值是对应的类。系统在运行时靠「查表」来决定实例化谁。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `genai_bench/scenarios/base.py` | 场景抽象基类 + 注册表 | `_registry`、`__init_subclass__`、`from_string`/`validate` |
| `genai_bench/scenarios/multimodal.py` 与 `scenarios/text.py` | 具体场景子类 | 新增场景的范本 |
| `genai_bench/sampling/base.py` | 采样器抽象基类 + 注册表 | `modality_registry`、`create`、`supports_task` |
| `genai_bench/sampling/text.py` | 文本采样器实现 | 新增任务的「低摩擦」改法 |
| `genai_bench/user/base_user.py` | User 基类 + Locust 集成 | `supported_tasks`、`sample`、`collect_metrics` 契约 |
| `genai_bench/user/openai_user.py` | OpenAI 兼容后端参考实现 | 新增后端的范本（`BACKEND_NAME`/`on_start`/`@task`/`send_request`） |
| `genai_bench/cli/validation.py` | CLI 校验与 **后端注册表** | `API_BACKEND_USER_MAP`、`validate_api_backend`、`validate_task` |
| `genai_bench/auth/unified_factory.py` | 认证统一工厂 | 新后端需在此登记认证 provider |
| `genai_bench/cli/cli.py` | benchmark 主流程 | 把 `auth_provider`/`host`/`api_backend` 注入 User 类 |
| `docs/developer-guide/adding-new-features.md` | 官方扩展文档 | 把它的步骤对应到上面的真实代码 |

## 4. 核心概念与源码讲解

### 4.1 统一心智模型：注册表驱动的三个扩展点

#### 4.1.1 概念说明

genai-bench 把「可配置的多样性」拆成了三个相对独立的维度，分别对应三个扩展点：

- **场景（Scenario）**：一个请求的输入/输出**规模**是多大（多少 token、多大图）。
- **采样器（Sampler）**：一种**输入模态**（text / image / ……）的请求怎么被造出来。
- **后端用户（User）**：一个**模型服务厂商**（openai / aws-bedrock / ……）的请求怎么发、响应怎么解析。

这三个点的「扩展摩擦」并不相同，这是本讲最重要的一句话：

> **场景和采样器靠 `__init_subclass__` 自动注册（定义即生效）；后端 User 没有自动注册，必须在多处手动登记。**

我们可以用一个「手动登记步数」来粗略量化三者的扩展成本。设 \( n_{\text{auto}} \) 为框架自动完成的登记数、\( n_{\text{manual}} \) 为你必须手写登记的点数，则：

\[
\text{扩展成本} \;\propto\; n_{\text{manual}}, \qquad
\begin{cases}
\text{场景：} & n_{\text{manual}} \approx 0 \text{（仅新增类型字母时需加枚举成员）}\\
\text{采样器（新任务）：} & n_{\text{manual}} \approx 1\text{–}2\\
\text{后端 User：} & n_{\text{manual}} \ge 3
\end{cases}
\]

记住这条曲线，后面三个模块就是在逐个解释它。

#### 4.1.2 核心流程

三类扩展点的「被发现」流程可统一抽象为：

```text
开发者定义一个子类
   │
   ├─ 场景/采样器：父类 __init_subclass__ 触发 → 自动写入注册表
   │     └─ 运行时 from_string / create 查表 → 实例化
   │
   └─ 后端 User：无自动机制 → 你手动写入 API_BACKEND_USER_MAP（+ 认证工厂）
         └─ CLI validate_api_backend 查表 → 把类放进 ctx.obj["user_class"]
```

关键差别只在「写入注册表」这一步是自动还是手动。

#### 4.1.3 源码精读

三张注册表的定义位置：

- 场景注册表 [genai_bench/scenarios/base.py:55-67](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L55-L67)：`_registry: Dict[str, Type["Scenario"]] = {}` 是类级字典；`__init_subclass__` 在子类定义时执行 `cls._registry[cls.scenario_type.value] = cls`，**自动登记**。
- 采样器注册表 [genai_bench/sampling/base.py:19-26](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L19-L26)：`modality_registry: Dict[str, Type["Sampler"]] = {}` 同样靠 `__init_subclass__` 自动写入，键是 `cls.input_modality`。
- 后端注册表 [genai_bench/cli/validation.py:25-38](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L25-L38)：`API_BACKEND_USER_MAP` 是**手写**的字面量字典，没有任何自动机制——这正是后端扩展更「重」的根源。

注意后两者查表入口的差异：场景/采样器的工厂（`Scenario.from_string`、`Sampler.create`）都定义在各自基类里、自包含；而后端的查表入口 `validate_api_backend` 在 CLI 校验层 [genai_bench/cli/validation.py:257-270](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L257-L270)，把选中的类塞进 `ctx.obj["user_class"]`，再由主流程取用。

#### 4.1.4 代码实践

**实践目标**：在运行时亲眼看到三张注册表的内容，验证「自动注册」确实发生了。

**操作步骤**（在仓库根目录，已 `pip install -e .` 后）：

```python
# 文件：scripts/inspect_registries.py（示例脚本，可自行创建于任意位置）
from genai_bench.scenarios.base import Scenario
from genai_bench.sampling.base import Sampler
# 触发各子类模块的导入，__init_subclass__ 才会执行
import genai_bench.scenarios.text      # noqa: F401
import genai_bench.scenarios.multimodal  # noqa: F401
import genai_bench.sampling.text       # noqa: F401

print("Scenario 注册表:", sorted(Scenario._registry.keys()))
print("Sampler 注册表:", sorted(Sampler.modality_registry.keys()))
```

**需要观察的现象**：两张自动注册表的键分别是场景字母集合（如 `A, D, E, I, N, R, U, dataset`）与输入模态集合（如 `image, text`）。

**预期结果**：注意 `V`（VIDEO）**不会**出现在场景注册表里——因为枚举里虽然有 `MultiModality.VIDEO = "V"`，却没有对应的子类去触发 `__init_subclass__`（详见 4.2）。

> 说明：此脚本仅为观察注册表，不触发真实压测；具体打印内容「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么「枚举里有 `V` 但注册表里没有 `V`」？

**参考答案**：注册表的键来自 `__init_subclass__` 里 `cls.scenario_type.value`，而 `__init_subclass__` 只在「某个 `Scenario` 子类被定义」时触发。枚举成员 `VIDEO = "V"` 只是定义了一个值，并没有定义子类，所以没人替它登记。

**练习 2**：如果想让一个新后端「定义即生效」，最少要改哪一处？

**参考答案**：做不到完全自动。后端 User 没有 `__init_subclass__` 注册机制，至少要手动把它加进 `API_BACKEND_USER_MAP`。

---

### 4.2 新增场景（Scenario）

#### 4.2.1 概念说明

场景回答的是「**这次请求要造多大规模的输入输出**」。当你想压测一种全新的规模描述方式（比如「按泊松分布采样输入 token」「按帧数描述视频」），就需要新增一个场景子类。

得益于 `__init_subclass__`，**新增场景子类本身是零摩擦的**——定义完即被注册，立刻能被 `Scenario.from_string` 解析。唯一的「手动」前置是：如果这个场景用的是一个**全新的类型字母**，你得先在某个枚举里加一个成员，作为 `scenario_type` 的取值。

#### 4.2.2 核心流程

新增一个场景子类的固定五步：

1. （仅新字母时）在 `scenarios/base.py` 的某个枚举里加成员，例如 `POISSON = "P"`。
2. 写一个继承 `Scenario` 的子类，设置类属性 `scenario_type = <那个枚举成员>` 和 `validation_pattern`（正则）。
3. 实现 `__init__` 保存参数。
4. 实现三个抽象方法：`sample()`（返回规模）、`to_string()`（逆序列化）、`parse()`（从参数串构造）。
5. 确保**该子类所在模块被 import 过**（否则 `__init_subclass__` 不触发）——通常靠主流程或采样器侧的 import 链自然达成。

解析时的路由是「取前缀字母 → 查 `_registry` → 委派 `parse`」，可写成分段函数：

\[
\text{route}(s) =
\begin{cases}
\text{raise UnknownType}, & \text{前缀} \notin \_registry\\
\text{raise BadPattern}, & \text{不匹配 } validation\_pattern\\
\text{Subclass.parse}(s[\text{len}(\text{前缀}):]), & \text{否则}
\end{cases}
\]

#### 4.2.3 源码精读

抽象契约与自动注册在 [genai_bench/scenarios/base.py:49-107](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L49-L107)：基类声明了 `sample`/`to_string`/`parse` 三个抽象方法，并用 `from_string` 工厂做路由——先用正则 `^([A-Za-z]+)` 取出类型字母，再 `cls._registry.get(type_token)` 查表。

一个范本是 `DeterministicDistribution`（`D(100,100)`）[genai_bench/scenarios/text.py:131-157](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/text.py#L131-L157)：它的 `scenario_type = TextDistribution.DETERMINISTIC`、`validation_pattern = r"^D\(\d+,\d+\)$"`，`sample()` 直接返回常量、`parse()` 用 `parse_params_str` 解析括号里的整数。

多模态场景的范本是 `ImageModality`（`I(512,512)`）[genai_bench/scenarios/multimodal.py:31-93](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/multimodal.py#L31-L93)，它的正则 `r"^I\(\d+,\d+(?:,\d+)?\)$"` 支持可选的第三个参数（图片数量），`parse()` 用 `*optional` 收集可变参数——这是「同一字母扩展参数」的标准写法。

「枚举有成员但无子类 = 未注册」的反面教材在枚举定义处 [genai_bench/scenarios/base.py:33-41](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L33-L41)：`MultiModality.VIDEO = "V"` 存在，但全仓库没有 `VideoModality(Scenario)` 子类，所以 `V` 永远进不了 `_registry`。校验逻辑 [genai_bench/scenarios/base.py:117-123](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py#L117-L123) 会因此对 `"V(640,480)"` 抛出 `Unknown type 'V'`。

#### 4.2.4 代码实践

**实践目标**：亲手把「缺失的 `V`（视频场景）」补全，体会「定义即注册」。

**操作步骤**：

1. 先复现缺口（示例脚本）：

   ```python
   from genai_bench.scenarios.base import Scenario
   import genai_bench.scenarios.multimodal  # noqa: F401
   try:
       Scenario.validate("V(640,480)")
   except ValueError as e:
       print("预期报错:", e)   # 期望看到 Unknown type 'V'
   ```

2. 新增一个子类（示例代码，可放在 `genai_bench/scenarios/multimodal.py` 末尾或你自己的模块里，但必须被 import 到）：

   ```python
   # 示例代码：新增视频场景
   from genai_bench.scenarios.base import MultiModality, Scenario, parse_params_str

   class VideoModality(Scenario):
       """V(width,height,frames) 视频输入场景。"""
       scenario_type = MultiModality.VIDEO
       validation_pattern = r"^V\(\d+,\d+,\d+\)$"

       def __init__(self, width: int, height: int, frames: int):
           self.width, self.height, self.frames = width, height, frames

       def sample(self):
           return (self.width, self.height), self.frames

       def to_string(self) -> str:
           return f"V({self.width},{self.height},{self.frames})"

       @classmethod
       def parse(cls, params_str: str) -> "VideoModality":
           w, h, frames = parse_params_str(params_str)[0]
           return cls(w, h, frames)
   ```

3. 确保该模块被 import，再运行 `Scenario.from_string("V(640,480,30)").sample()`。

**需要观察的现象**：补全前 `validate("V(640,480,30)")` 报 `Unknown type 'V'`；补全后能成功解析并返回 `((640, 480), 30)`。

**预期结果**：`V` 出现在 `Scenario._registry` 的键里，路由成功。注意：要让 CLI 的 `--traffic-scenario V(...)` 也接受它，还需要让采样器在 `_validate_scenario` 里承认这个类型——这部分属于 4.3 的范畴。运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果只想给已有的 `I(...)` 增加第四个参数 `fps`，需要新增子类吗？

**参考答案**：不需要。直接修改 `ImageModality` 的 `validation_pattern` 与 `parse` 即可（参考它已用 `*optional` 处理可变参数的写法），因为字母 `I` 已经注册。

**练习 2**：为什么新增场景子类后，必须「被 import」才生效？

**参考答案**：`__init_subclass__` 在子类**定义语句执行时**触发，而定义语句只有在模块被 import 时才会执行。未被 import 的子类从未被定义，自然不会进注册表。

---

### 4.3 新增任务与采样器（Sampler）

#### 4.3.1 概念说明

采样器回答的是「**给定一种输入模态，如何把场景+数据集揉成一个 `UserRequest`**」。这里要区分两种「新增」：

- **新增一个已有输入模态下的新任务**（例如给 `text` 加 `text-to-audio-summary`）：这是**低摩擦**的——通常不需要新采样器，只在现有采样器里加一行 `supported_tasks` 成员，再加一个请求构造分支。
- **新增一种全新输入模态**（例如 `audio` 输入）：这才是**高摩擦**的，需要新建一个 `Sampler` 子类。

官方文档 `docs/developer-guide/adding-new-features.md` 的第 2 节正是按这两种情况分别给出指引的。

#### 4.3.2 核心流程

采样器的「任务路由」是**两级查表**：

```text
Sampler.create("text-to-X")
   │  ① 按 input_modality="text" 查 modality_registry  → TextSampler
   │  ② supports_task("text","X") 即检查 "text-to-X" in TextSampler.supported_tasks
   └─ 实例化 TextSampler(output_modality="X")
         └─ sample() 里再按 output_modality 分发到具体构造方法
```

所以新增一个 `text-to-X` 任务，至少要动两处：

1. 在 `TextSampler.supported_tasks` 集合里加 `"text-to-X"`（否则 `supports_task` 返回 `False`，`create` 直接报错）。
2. 在 `TextSampler.sample()` 的 `output_modality` 分发里加一个分支，或新增一个 `_sample_X_request` 方法。

如果是全新输入模态，则额外要：新建子类、设 `input_modality` 与 `supported_tasks`、实现 `sample()`（定义即注册到 `modality_registry`）。

#### 4.3.3 源码精读

工厂与校验在 [genai_bench/sampling/base.py:82-129](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L82-L129)：`create` 先 `task.split("-to-")` 拆出输入/输出模态，查 `modality_registry`（对 `image-text` 这类复合模态有「回落到 `image`」的兼容），再用 `supports_task` 校验输出模态是否被支持；`supports_task` 的实现就是 `task_name in cls.supported_tasks`。

「新增任务」的范本是 `TextSampler` [genai_bench/sampling/text.py:25-87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L25-L87)：`input_modality = "text"` 触发自动注册；`supported_tasks` 是一个**集合**（行 33-39），列出 `text-to-text`、`text-to-embeddings`、`text-to-rerank`、`text-to-image`、`text-to-speech`；`sample()`（行 65-87）用一个 `if/elif` 按 `output_modality` 分发到 `_sample_chat_request` 等私有方法。官方文档明确建议：**不要无限堆 `if-else`**，必要时抽请求构造器（源码注释里也留了 `TODO: create Delegated Request Creator`）。

注意 `sample()` 里还耦合了「场景模式 vs 数据集模式」的二分（`_is_dataset_mode`），以及输出模态与场景类型的强校验 `_validate_scenario` [genai_bench/sampling/text.py:260-300](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L260-L300)——新增输出模态时，这里也要加一个 `elif` 分支，否则会因场景类型不匹配而报错。

#### 4.3.4 代码实践

**实践目标**：为 `TextSampler` 增加一个（仅用于学习、不真正发请求的）假任务 `text-to-x`，走通「低摩擦新增」全流程。

**操作步骤**：

1. 在 `TextSampler.supported_tasks` 集合里追加 `"text-to-x"`（示例代码，仅本地实验，勿提交）：

   ```python
   supported_tasks = {
       "text-to-text", "text-to-embeddings", "text-to-rerank",
       "text-to-image", "text-to-speech", "text-to-x",  # 新增
   }
   ```

2. 在 `sample()` 的分发里加一个最小分支，复用现有 chat 构造（示例代码）：

   ```python
   elif self.output_modality == "x":
       return self._sample_chat_request(scenario)  # 先复用，验证链路
   ```

3. 验证工厂能选中它：

   ```python
   from genai_bench.sampling.base import Sampler
   import genai_bench.sampling.text  # noqa: F401
   print(Sampler.modality_registry["text"].supports_task("text", "x"))  # 期望 True
   ```

**需要观察的现象**：加之前 `supports_task("text","x")` 返回 `False`（`create` 会抛 `does not support output modality: x`）；加之后返回 `True`。

**预期结果**：仅改 `supported_tasks` + 一个 `elif` 分支，就完成了任务在采样器侧的注册；输入模态 `text` 仍由 `__init_subclass__` 自动登记，无需手动。运行结果「待本地验证」。

> 注意：要让该任务在 CLI 端真正可用，还需在 `DEFAULT_SCENARIOS_BY_TASK` 给它配默认场景、并在某个 User 的 `supported_tasks` 里登记方法（见 4.4）。这里只验证采样器一环。

#### 4.3.5 小练习与答案

**练习 1**：为什么「新增 `text-to-x`」不需要新建 Sampler 子类？

**参考答案**：因为注册表 `modality_registry` 的键是**输入模态**，`text` 已经由 `TextSampler` 注册。新任务共享同一输入模态，只需在现有采样器里扩展 `supported_tasks` 与分发逻辑。

**练习 2**：如果新建了一个 `AudioSampler` 但忘了 import 它，`Sampler.create("audio-to-text")` 会怎样？

**参考答案**：`modality_registry` 里没有 `"audio"`（`__init_subclass__` 未触发），`create` 走到 `raise ValueError("No sampler supports input modality: audio")`。

---

### 4.4 新增后端（User）

#### 4.4.1 概念说明

后端 User 回答的是「**怎么把请求发给某个具体厂商的服务、又怎么把它的响应解析回 `UserResponse`**」。这是三个扩展点里**最重**的：它没有 `__init_subclass__` 自动注册，牵涉 CLI、认证、主流程三处协同。新增后端有两种策略（u3-l3 已建立）：

- **复用继承**：若新后端是 OpenAI 兼容协议（如自家的推理网关），直接继承 `OpenAIUser`，只覆写 `BACKEND_NAME` 与需要的差异点（`vllm`/`sglang`/`oci-openai` 走这条路）。
- **从零实现**：若协议差异大（如 AWS Bedrock 的 boto3 SDK），继承 `BaseUser` 自己写发送与解析（`AWSBedrockUser` 走这条路）。

不论哪种，复用下限都是 `BaseUser` 提供的 `sample()` 与 `collect_metrics()`。

#### 4.4.2 核心流程

新增后端 User 的完整登记清单（这是本模块的核心）：

1. **写 User 子类**：设类属性 `BACKEND_NAME`（`--api-backend` 的合法取值，即身份证）与 `supported_tasks`（任务字符串 → 方法名）；为 `text-to-text` 实现一个带 `@task` 装饰器的方法，方法体走「`sample()` → 组装 payload → 发送 → 解析为 `UserResponse` → `collect_metrics()`」。
2. **登记后端注册表**：在 `API_BACKEND_USER_MAP` 里加 `YourUser.BACKEND_NAME: YourUser`（**手动**，无自动机制）。
3. **登记认证**：在 `UnifiedAuthFactory.create_model_auth` 加一个 `elif provider == "..."` 分支（或在 `cli.py` 的 `auth_backend_map` 里把新后端别名映射到已有 provider，复用现成认证）。
4. **配认证参数**：在 `cli.py` 的 `benchmark` 函数里为新后端补 `auth_kwargs` 分支（若复用 openai 认证可省）。
5. （可选）更新 `validate_api_key` 对该后端的密钥要求。

被选中的链路是：`--api-backend foo` → `validate_api_backend` 查表 → 存 `ctx.obj["user_class"]` → 主流程把 `auth_provider`/`host`/`api_backend` 注入该类的类属性 → Locust 实例化每个虚拟用户时，`on_start` 据这些属性建客户端。

#### 4.4.3 源码精读

**基类契约**在 [genai_bench/user/base_user.py:12-92](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L12-L92)：

- `supported_tasks: Dict[str, str] = {}`（行 13）是任务→方法名的路由表，`is_task_supported`（行 23-25）仅做成员判断。
- `__new__` 守卫（行 15-18）禁止直接实例化 `BaseUser`。
- `sample()`（行 27-44）从 `environment.sampler.sample(environment.scenario)` 取请求。
- `collect_metrics()`（行 46-92）是每个任务方法**必须**在最后调用的出口——它把 `UserResponse` 换算成指标并上报。

**参考实现 `OpenAIUser`** 在 [genai_bench/user/openai_user.py:32-56](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L32-L56)：`BACKEND_NAME = "openai"`、`supported_tasks` 把六种任务映射到方法名；类属性 `host`/`auth_provider`/`headers`（行 43-45）由主流程注入；`on_start` 用 `auth_provider.get_headers()` 构建 HTTP 头。

**任务方法范本 `chat`** 在 [genai_bench/user/openai_user.py:58-123](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L58-L123)：`@task` 装饰、`self.sample()` 取请求、组装 `payload`、最后委托 `send_request`。统一的发送中枢 `send_request` 在 [genai_bench/user/openai_user.py:311-377](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L311-L377)：它把「发请求」与「解析」用 `parse_strategy` 回调解耦，并用异常兜底把网络错误翻译成带状态码的 `UserResponse`，**最后一句必然是 `self.collect_metrics(metrics_response, endpoint)`**（行 376）——这是所有任务方法的收尾契约。

**后端注册表**（手动）在 [genai_bench/cli/validation.py:25-38](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L25-L38)：注意 `"vllm"` 与 `"sglang"` 是指向 `OpenAIUser` 的**别名**，不是新类——这是「复用继承」策略的极致体现。查表与写入上下文在 [genai_bench/cli/validation.py:257-270](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L257-L270)。`validate_task` 在 [genai_bench/cli/validation.py:313-352](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L313-L352) 用 `is_task_supported` 校验任务兼容，并把方法对象存进 `ctx.obj["user_task"]`（行 350）。

**认证登记**在 [genai_bench/auth/unified_factory.py:31-99](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L31-L99)：`create_model_auth` 是一串 `if/elif provider == ...`，未知 provider 抛 `ValueError` 并列出合法取值。

**主流程注入**在 [genai_bench/cli/cli.py:251-284](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L251-L284)：`auth_backend_map`（行 251-259）把别名归一化到认证 provider；随后 `user_class.auth_provider = auth_provider`、`user_class.host = api_base`、`user_class.api_backend = api_backend`（行 282-284）三个赋值，把运行时配置挂到**类**上——这就是为什么 `OpenAIUser` 要把它们声明为类属性。

#### 4.4.4 代码实践

**实践目标**：仿照 `OpenAIUser` 写一个极简 `CustomUser`（仅 `text-to-text`），并列出让 `--api-backend custom` 选中它的全部注册点。采用「复用继承」策略，把摩擦降到最低。

**操作步骤**：

1. 新建 `genai_bench/user/custom_user.py`（示例代码）：

   ```python
   # 示例代码：一个 OpenAI 兼容的极简自定义后端
   from genai_bench.user.openai_user import OpenAIUser

   class CustomUser(OpenAIUser):
       BACKEND_NAME = "custom"
       supported_tasks = {
           "text-to-text": "chat",   # 复用父类 chat，无需重写
       }
   ```

   因为继承自 `OpenAIUser`，`on_start`、`chat`、`send_request`、`parse_chat_response`、`collect_metrics` 全部复用——这是「极简」的关键。

2. **注册点 ①（必改）**：在 `genai_bench/cli/validation.py` 的 `API_BACKEND_USER_MAP` 里加一行（示例代码）：

   ```python
   from genai_bench.user.custom_user import CustomUser
   API_BACKEND_USER_MAP = {
       ...
       CustomUser.BACKEND_NAME: CustomUser,   # 新增
   }
   ```

3. **注册点 ②（认证，二选一）**：
   - 最省事：在 `cli.py` 的 `auth_backend_map` 里加 `"custom": "openai"`，复用 OpenAI 的 API Key 认证，无需改认证工厂。
   - 或在 `UnifiedAuthFactory.create_model_auth` 加 `elif provider == "custom": ...` 分支自建认证。

**需要观察的现象**：改完后执行 `genai-bench benchmark --api-backend custom --task text-to-text --model-tokenizer <tok> ...`，`validate_api_backend` 应能查到 `CustomUser`、`validate_task` 应通过 `is_task_supported("text-to-text")`。

**预期结果**：`--api-backend custom` 被识别；由于复用了 OpenAI 协议，请求会以 OpenAI chat 格式发往 `--api-base` 指定的地址。若不配认证映射，会因 `create_model_auth("custom")` 抛 `Unsupported model provider` 而失败——这正好验证了注册点 ② 的必要性。**真实运行结果「待本地验证」。**

#### 4.4.5 小练习与答案

**练习 1**：假如 `CustomUser` 只继承了 `BaseUser`（而非 `OpenAIUser`），它必须自己实现哪些东西？

**参考答案**：至少要实现 `on_start`（建客户端/请求头）、一个 `@task` 装饰的任务方法（含 `sample()` → 组装 payload → 发送 → 解析为 `UserResponse`），并在该方法末尾调用 `self.collect_metrics(resp, endpoint)`。它无法复用 `send_request`/`parse_chat_response`（这些是 `OpenAIUser` 的方法，不在 `BaseUser` 上）。

**练习 2**：为什么主流程把 `host`/`auth_provider` 赋值给**类**（`user_class.host = ...`），而不是实例？

**参考答案**：因为赋值发生在 Locust 实例化虚拟用户**之前**；类属性会被每个实例继承，从而让每个虚拟用户的 `on_start` 都能读到同一份配置。这是 Locust「先配类、后生实例」工作流的必然要求。

---

## 5. 综合实践

把三个扩展点串起来，完成一个微型二次开发：**让 genai-bench 支持 `--api-backend custom` 跑 `text-to-text`，并使用一个自定义场景 `D2(50,50)`（一种新的确定性变体，仅用于练手）**。

建议步骤：

1. **新增场景**（4.2）：在 `scenarios/text.py` 增加一个 `DeterministicAlt` 子类，`scenario_type = TextDistribution.DETERMINISTIC` 会与 `D` 冲突——所以先在 `scenarios/base.py` 的 `TextDistribution` 枚举里加成员 `DETERMINISTIC_ALT = "D2"`，再写子类（`validation_pattern = r"^D2\(\d+,\d+\)$"`，`sample` 返回常量）。验证 `Scenario.from_string("D2(50,50)")` 可解析。

2. **接入采样器**（4.3）：由于新场景的 `scenario_type` 是 `TextDistribution`，`TextSampler._validate_scenario` 对 `output_modality == "text"` 的校验天然通过，无需改采样器。用 `TextSampler.create("text-to-text", ...).sample(Scenario.from_string("D2(50,50)"))` 验证能产出 `UserChatRequest`。

3. **新增后端**（4.4）：按 4.4.4 的步骤实现 `CustomUser(OpenAIUser)` 并完成注册点 ①、②。

4. **端到端**：执行

   ```bash
   genai-bench benchmark \
     --api-backend custom --task text-to-text \
     --model <model> --model-tokenizer <tokenizer> \
     --api-base <your-openai-compatible-endpoint> \
     --traffic-scenario "D2(50,50)" \
     --num-concurrency 1 --max-time-per-run 1
   ```

   观察 `experiments/` 下是否生成正常 run JSON（说明请求发出且指标回流），以及 `--api-backend custom` 是否被接受。

5. **记录**：用一张表总结你改动的文件与对应的「注册点类型」（自动 / 手动）。

> 提示：本实践涉及多处源码改动，仅供本地学习，**请勿提交到主线**。涉及真实服务调用的结果「待本地验证」。如果只想验证扩展机制而不发真实请求，可在第 4 步用 `--api-base` 指向一个本地 mock 服务（如返回固定 SSE 的简易服务），重点确认「注册点是否齐全、CLI 是否识别新后端与新场景」。

## 6. 本讲小结

- genai-bench 有三个扩展点：**场景（规模）、采样器（输入模态→请求）、后端 User（厂商协议）**，分别由 `scenarios/`、`sampling/`、`user/` 三个包承载。
- **场景与采样器靠 `__init_subclass__` 自动注册**（定义即生效）；**后端 User 无自动注册**，必须手动登记——这是三者扩展成本递增的根本原因。
- 新增场景：设 `scenario_type`（必要时先加枚举成员）+ `validation_pattern` + 实现 `sample`/`to_string`/`parse`，并确保模块被 import；「枚举有成员但无子类」（如 `V`）不会被注册。
- 新增任务：复用输入模态时只需改 `supported_tasks` + 加 `sample()` 分发分支；新增输入模态才需新建 Sampler 子类。
- 新增后端 User：写子类（`BACKEND_NAME` + `supported_tasks` + `@task` 方法，末尾调 `collect_metrics`）→ 登记到 `API_BACKEND_USER_MAP` → 登记认证（工厂或 `auth_backend_map`）→ 配 `auth_kwargs`；可走「复用继承」（继承 `OpenAIUser`）或「从零实现」（继承 `BaseUser`）。
- 主流程通过给**类**赋值 `auth_provider`/`host`/`api_backend` 把运行时配置下发给每个虚拟用户。

## 7. 下一步学习建议

- **吃透主流程**：本讲的「注册点」都散落在 CLI 与主流程里，建议接着读 u8-l1（benchmark 主流程编排），看 `user_class` 是如何从校验层一路流到 Locust 实例化的。
- **认证深入**：若你的新后端需要非 API-Key 认证，参考 u5-l1（认证体系总览）与 u5-l2（模型认证 Provider 实现），学习如何实现 `ModelAuthProvider` 的三个抽象方法并接入适配器。
- **测试与发布**：官方文档 `adding-new-features.md` 第 4、5 步要求为新功能补测试与文档，可结合 u8-l4（测试体系、CI 与发布）了解 `tests/` 的组织与 `pytest` 约定。
- **源码阅读顺序建议**：先重读 `user/openai_user.py` 全文（最完整的后端范本），再对照 `user/aws_bedrock_user.py`（从零实现的范本），最后回到 `docs/developer-guide/adding-new-features.md`，逐条把文档步骤映射到你现在已经认识的源码位置。
