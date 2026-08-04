# Recipe 编码压缩指令

## 1. 本讲目标

本讲精读 `Recipe` 模型——它是把「我想怎么压缩这个模型」翻译成「机器能执行的一串动作」的中间层。学完后你应该能够：

- 说出 `Recipe` 的三种核心字段 `args` / `stage` / `modifiers` 各自承载什么。
- 掌握 `Recipe.create_instance` 的多源分派逻辑：同一个方法能吃下文件路径、YAML/JSON 字符串、单个 `Modifier` 实例、`Modifier` 列表，甚至一个现成的 `Recipe`。
- 读懂 YAML recipe 里 `xxx_stage` / `yyy_modifiers` 这种「后缀命名约定」，并能手写出一段合法的 recipe。
- 理解 recipe 如何序列化回 YAML、如何与已有 recipe 文件合并、以及如何按 stage 过滤。
- 理解「AWQ 必须后接量化 modifier」这条顺序校验规则的产生时机与原理。

本讲承接 [u2-l3 Modifier 基类生命周期] 和 [u2-l4 ModifierFactory 自动发现与注册]：你已经知道每个 modifier 是一个带生命周期钩子的对象，也知道 `ModifierFactory` 能按字符串类名实例化 modifier。本讲把它们串成一条完整链路——**一段文本 recipe，是怎么一步步变成一组真实 `Modifier` 对象的**。

## 2. 前置知识

在进入源码前，先用三个生活化的比喻建立直觉。

**比喻一：菜谱与工序。** `Recipe` 直译就是「菜谱」。一份菜谱写明了「先做什么、再做什么」的有序步骤；每个步骤就是一个 `Modifier`（修饰器/动作）。比如「先 SmoothQuant 平滑，再 GPTQ 量化」就是两道工序，顺序不能乱。

**比喻二：表单分箱。** 一份 YAML recipe 是一张层次分明的表单，分三层抽屉：

- 第一层是 **stage（阶段）**，键名以 `_stage` 结尾，例如 `test_stage`、`default_stage`。一个 recipe 可以有多个 stage，运行时挑一个用。
- 第二层是 **group（分组）**，键名以 `_modifiers` 结尾，例如 `quantization_modifiers`、`pruning_modifiers`。group 主要是给同类的动作归个类。
- 第三层才是真正的 **modifier**，键是类名（如 `QuantizationModifier`），值是它的参数字典。

**比喻三：翻译官。** `Recipe` 是一个双向翻译官：

- 正向（解析）：把人写的 YAML 文本翻译成 Python 里的 `Modifier` 对象列表。
- 反向（序列化）：把内存里的 `Modifier` 对象列表翻译回 YAML 文本，可存盘、可 diff。

还需要两个已学概念（来自 u2-l4）：

- `ModifierFactory.create(type_, **kwargs)`：按类名字符串实例化一个 modifier，是「文本→对象」的真正执行点。
- `Modifier` 是 pydantic 模型，每个 modifier 都带一个 `group: str | None` 字段，用于标记它属于哪个分组。

最后补一个术语：**PTQ（Post-Training Quantization，训练后量化）** 指模型训练好之后再做量化，`oneshot` 就是 PTQ 的主入口（见 u1-l4）。本讲讨论的 recipe 几乎都服务于 PTQ 流程。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/llmcompressor/recipe/recipe.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py) | `Recipe` 数据模型本体，含 `create_instance` / `from_modifiers` / `from_dict` / `dict` / `yaml` 以及顺序校验 `validate_model_after`。 |
| [src/llmcompressor/recipe/utils.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/utils.py) | recipe 的工具函数：字符串解析、Markdown front-matter 提取、序列化为字典、stage 过滤、recipe 字典合并。 |
| [src/llmcompressor/recipe/\_\_init\_\_.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/__init__.py) | 包出口，把 `Recipe` 与三个类型别名 `RecipeInput` / `RecipeStageInput` / `RecipeArgsInput` 提升为公开 API。 |
| [tests/llmcompressor/recipe/test_recipe.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/recipe/test_recipe.py) | recipe 的单元测试，提供了大量可直接运行的合法/非法 recipe 字符串样例，是本讲实践任务的事实依据。 |

> 链接说明：本讲所有永久链接均指向固定 commit `2d7a7ea0`，行号随该 commit 固定。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：① Recipe 数据模型与多源输入总览 → ② `create_instance` 多源分派 → ③ `from_dict` 命名约定与实例化 → ④ 序列化与合并 → ⑤ AWQ 顺序校验。

### 4.1 Recipe 数据模型与多源输入总览

#### 4.1.1 概念说明

`Recipe` 是一个 pydantic `BaseModel`，它**本身不执行任何压缩**，只是一个「容器 + 翻译器」：它持有最终解析好的 `modifiers` 列表，并提供把各种形态的输入「归一化」成这个列表的方法。

它的核心矛盾是：用户传入的 recipe 形态五花八门——可能是一个 `.yaml` 文件路径，可能是一段 YAML 字符串，可能直接就是一个或多个 `Modifier` 实例，甚至可能已经是 `Recipe` 对象。`Recipe` 的设计目标就是**用一个统一入口把这些异构输入全部归一**。

#### 4.1.2 核心流程

```
用户输入（异构）
     │
     ▼
Recipe.create_instance(...)   ← 统一入口（总翻译官）
     │
     ├── 输入是 Recipe?        → 原样返回（已经是成品）
     ├── 输入是 Modifier/list? → from_modifiers（对象→容器）
     ├── 输入是文件路径?       → 读文件 + from_dict（文本→对象）
     └── 输入是字符串?         → from_dict（文本→对象）
     │
     ▼
Recipe(args=..., stage=..., modifiers=[Modifier, Modifier, ...])
```

#### 4.1.3 源码精读

`Recipe` 的三个字段定义在类顶部：

[src/llmcompressor/recipe/recipe.py:38-42](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L38-L42) —— `args` 是任意参数字典（运行时由 `recipe_args` 注入），`stage` 默认 `"default"`，`modifiers` 是最终的 `Modifier` 对象列表，`model_config` 允许任意类型字段（因为 modifier 类型不固定）。

文件末尾定义了三个类型别名，描述「recipe 参数到底接受哪些形态」：

[src/llmcompressor/recipe/recipe.py:285-287](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L285-L287) —— `RecipeInput` 是 `oneshot(recipe=...)` 形参的类型，它允许：字符串（路径或 YAML）、字符串列表、`Recipe`、`Recipe` 列表、单个 `Modifier`、`Modifier` 列表。这就是「多源」的形式化表达。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 `RecipeInput` 接受的多种形态。
2. **操作步骤**：在 Python 里分别用「YAML 字符串」和「Modifier 实例列表」两种方式构造 recipe。
   ```python
   # 示例代码
   from llmcompressor.recipe import Recipe
   from llmcompressor.modifiers.quantization import QuantizationModifier

   # 形态 A：YAML 字符串
   r_yaml = Recipe.create_instance(
       """
       test_stage:
           quantization_modifiers:
               QuantizationModifier:
                   scheme: FP8_DYNAMIC
                   targets: Linear
       """
   )

   # 形态 B：Modifier 实例列表
   r_obj = Recipe.create_instance(
       [QuantizationModifier(scheme="FP8_DYNAMIC", targets="Linear")]
   )

   print(type(r_yaml), len(r_yaml.modifiers))
   print(type(r_obj), len(r_obj.modifiers))
   ```
3. **需要观察的现象**：两种输入都返回 `Recipe` 实例，且 `.modifiers` 长度都是 1。
4. **预期结果**：`<class 'llmcompressor.recipe.recipe.Recipe'> 1`，两行一致。
5. 若本地无 GPU/无大依赖，此实践只需 `pip install llmcompressor` 后即可在 CPU 上运行；量化参数构造本身不触发推理。**待本地验证**具体打印字符串。

#### 4.1.5 小练习与答案

**练习 1**：`Recipe` 模型本身会不会去跑前向、会不会修改权重？
**答案**：不会。`Recipe` 只是容器与翻译器，真正修改模型的是它持有的 `Modifier` 对象在 lifecycle 事件中被触发的钩子（见 u2-l3）。

**练习 2**：`RecipeInput` 类型别名里为什么要把 `str` 和 `Recipe`、`Modifier` 并列？
**答案**：因为 `oneshot` 的 `recipe` 参数希望同时支持「写文本」「传对象」「传成品」三种使用习惯，类型别名把这三种合法形态显式枚举出来。

---

### 4.2 create_instance：多源创建总入口

#### 4.2.1 概念说明

`create_instance` 是 `Recipe` 最常用的类方法，也是 `oneshot` 内部（经 `lifecycle.initialize`）调用 recipe 的唯一入口。它的工作就是**分派（dispatch）**：判断输入属于哪一类，再路由到对应的解析路径。

#### 4.2.2 核心流程

`create_instance(path_or_modifiers, ...)` 的判断顺序（顺序很重要）：

```
1. isinstance(Recipe)?              → 直接返回（短路）
2. isinstance(Modifier | list)?     → from_modifiers(...)
3. os.path.isfile(...)?             → 读文件 → from_dict(...)
4. 否则当作字符串                   → _load_json_or_yaml_string → from_dict(...)
```

注意第 3、4 步都会经过 `filter_dict(obj, target_stage)`，用于按 `target_stage` 筛选 stage。

#### 4.2.3 源码精读

四段分派逻辑都在同一个方法里：

[src/llmcompressor/recipe/recipe.py:125-132](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L125-L132) —— 第 1、2 步：`Recipe` 原样返回；`Modifier` 或 `list` 走 `from_modifiers`。

[src/llmcompressor/recipe/recipe.py:134-143](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L134-L143) —— 第 4 步：当输入不是本地文件时，当作字符串处理，调用 `_load_json_or_yaml_string` 解析。

[src/llmcompressor/recipe/recipe.py:144-165](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L144-L165) —— 第 3 步：是文件时，按后缀分流：`.md` 走 Markdown front-matter 提取，`.json` 走 `json.loads`，`.yaml/.yml` 走 `yaml.safe_load`，其余后缀尝试通用解析。

字符串解析的工具函数在 utils 中：

[src/llmcompressor/recipe/utils.py:10-26](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/utils.py#L10-L26) —— `_load_json_or_yaml_string` 先试 JSON、再试 YAML，都失败则抛 `ValueError`；并校验结果必须是 `dict`（recipe 的顶层结构必须是字典）。

Markdown recipe 卡片的解析：

[src/llmcompressor/recipe/utils.py:29-53](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/utils.py#L29-L53) —— `_parse_recipe_from_md` 用正则提取 `--- ... ---` 包裹的 YAML front-matter，把 README 说明文字剥离掉，只留 recipe 本体。

而 `from_modifiers` 的有趣之处在于它先用 `tree_leaves` 把可能嵌套的列表「拍平」：

[src/llmcompressor/recipe/recipe.py:44-85](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L44-L85) —— 第 74 行 `modifiers = tree_leaves(modifiers)` 把嵌套结构（如 `[A, [B, C]]`）展平为 `[A, B, C]`。这是为兼容旧的 `AWQModifier`——它其实是一个**函数**，返回 `[AWQTransformModifier, QuantizationModifier]` 列表（见 [src/llmcompressor/modifiers/awq/\_\_init\_\_.py:58-83](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/awq/__init__.py#L58-L83)）。

#### 4.2.4 代码实践

1. **实践目标**：验证「同一个 modifier 列表」与「等价 YAML 字符串」解析出的 recipe 内容一致。
2. **操作步骤**：直接复现官方测试 `test_recipe_can_be_created_from_modifier_instances` 的思路：
   ```python
   # 示例代码
   from llmcompressor.recipe import Recipe
   from llmcompressor.modifiers.pruning.sparsegpt import SparseGPTModifier

   m = SparseGPTModifier(sparsity=0.5, group="pruning")

   # 形态 B：对象
   r_obj = Recipe.create_instance([m], modifier_group_name="dummy")

   # 形态 A：等价 YAML
   r_yaml = Recipe.create_instance(
       """
       dummy_stage:
           pruning_modifiers:
               SparseGPTModifier:
                   sparsity: 0.5
       """
   )

   a, b = r_obj.modifiers[0], r_yaml.modifiers[0]
   print(type(a) is type(b), a.model_dump() == b.model_dump())
   print(r_obj.stage, r_yaml.stage)
   ```
3. **需要观察的现象**：两个 modifier 类型相同、参数相同；两个 recipe 的 stage 都是 `dummy`。
4. **预期结果**：`True True`，`dummy dummy`。
5. **待本地验证**：`model_dump()` 的完整键值（受 `SparseGPTModifier` 字段集影响）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `create_instance` 要先判断「是不是文件」再判断「当作字符串」？反过来行不行？
**答案**：不行。字符串可能恰好是一个合法的 YAML 内容，也可能恰好长得像一个不存在的文件名。代码用 `os.path.isfile` 区分：是文件就按文件读，否则才当作字符串内容解析。若反过来，会把文件路径误当字符串解析而报错。

**练习 2**：旧的 `llmcompressor.modifiers.awq.AWQModifier` 是一个类吗？为什么 `from_modifiers` 要用 `tree_leaves`？
**答案**：不是类，是一个**函数**（兼容垫片），它返回 `[AWQTransformModifier, QuantizationModifier]` 列表。`tree_leaves` 把这种嵌套列表拍平成一维的 modifier 列表，保证后续流程拿到的是扁平的 `[Modifier, ...]`。

---

### 4.3 from_dict：stage/group 命名约定与工厂实例化

#### 4.3.1 概念说明

`from_dict` 是「文本→对象」的核心翻译器。它把一个普通 Python 字典（来自 YAML/JSON 解析结果）按照**后缀命名约定**逐层拆开，最终对每个 modifier 调用 `ModifierFactory.create` 把类名字符串变成真实对象。

理解三条约定位规则是本模块的关键：

1. 顶层键以 `_stage` 结尾 → 这是一个 stage，去掉后缀就是 stage 名（如 `test_stage` → `test`）。
2. stage 下的键以 `_modifiers` 结尾 → 这是一个 group，去掉后缀得到「推断的 group 名」（如 `quantization_modifiers` → `quantization`）。
3. group 下的每个键是 modifier 类名，值是参数字典；其中可选的 `group` 字段可以覆盖第 2 步推断的 group 名。

#### 4.3.2 核心流程

```
for 顶层键 stage_key:
    if stage_key 以 "_stage" 结尾:
        stage = stage_key 去掉 "_stage"
        for group_key in stage_value:
            if group_key 以 "_modifiers" 结尾:
                inferred_group = group_key 去掉 "_modifiers"
                for 类名, 参数字典 in group_value:
                    group = 参数字典.get("group", inferred_group)  # 显式覆盖推断值
                    modifier = ModifierFactory.create(类名, group=group, **参数)
                    modifiers.append(modifier)
```

注意：解析开始前会确保 `ModifierFactory._loaded`（即已扫描过 modifiers 子包，见 u2-l4），否则调用 `refresh()`。

#### 4.3.3 源码精读

[src/llmcompressor/recipe/recipe.py:167-204](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L167-L204) —— 这是 `from_dict` 的全部实现。几个要点：

- 第 180-181 行：懒加载工厂——首次解析时触发一次 `ModifierFactory.refresh()`，把所有算法类登记进注册表。
- 第 184 行：`stage_key.endswith("_stage")` 识别 stage。
- 第 187 行：`group_key.endswith("_modifiers")` 识别 group。
- 第 190 行：`group = mod_args.get("group", inferred_group)`——允许在 modifier 参数里显式写 `group: my_group` 来覆盖从键名推断的 group。
- 第 191-198 行：调用 `ModifierFactory.create(mod_type, group=group, allow_registered=True, allow_experimental=True, **mod_args)`，把字符串类名实例化（u2-l4 讲过的三级查找）。

这正好闭环了 u2-l4：`ModifierFactory` 负责「类名→对象」，而 `Recipe.from_dict` 负责「文本→类名 + 参数」。

#### 4.3.4 代码实践

1. **实践目标**：手写一段带两个 group 的 YAML recipe，验证命名约定与 `group` 覆盖行为。
2. **操作步骤**：
   ```python
   # 示例代码
   from llmcompressor.recipe import Recipe

   recipe_str = """
   test_stage:
       quantization_modifiers:
           QuantizationModifier:
               scheme: FP8_DYNAMIC
               targets: Linear
               ignore: lm_head
       pruning_modifiers:
           WandaModifier:
               sparsity: 0.5
               group: special_group
   """
   r = Recipe.create_instance(recipe_str)
   for m in r.modifiers:
       print(type(m).__name__, "->", getattr(m, "group", None))
   print("stage =", r.stage)
   ```
3. **需要观察的现象**：`QuantizationModifier` 的 group 应为从键名推断的 `quantization`；`WandaModifier` 的 group 应为显式覆盖的 `special_group`；stage 为 `test`。
4. **预期结果**：
   ```
   QuantizationModifier -> quantization
   WandaModifier -> special_group
   stage = test
   ```
5. **待本地验证**：若当前版本 `WandaModifier` 的必填字段不止 `sparsity`，可能需要补参数才能构造成功。

#### 4.3.5 小练习与答案

**练习 1**：如果 YAML 里某个 group 键写成 `quantization_modifier`（少了 `s`），会发生什么？
**答案**：因为它不以 `_modifiers` 结尾，`from_dict` 会**静默跳过**它，该 group 下的所有 modifier 都不会被实例化，最终 `modifiers` 列表为空。这是一个容易踩的拼写陷阱。

**练习 2**：`from_dict` 为什么在解析前要检查 `ModifierFactory._loaded`？
**答案**：工厂的自动发现是懒加载的（u2-l4）。如果在工厂还没扫描过 `llmcompressor.modifiers` 子包时就调用 `create`，注册表是空的，所有类名都查不到。所以 `from_dict` 必要时先 `refresh()` 一次，保证类名可被解析。

---

### 4.4 序列化、合并与 stage 过滤

#### 4.4.1 概念说明

recipe 不只要能「读进来」，还要能「写出去」。`Recipe` 提供两条反向通路：

- `dict()`：把 `modifiers` 列表重新组装回符合「stage / group / modifier」三层结构的普通字典。
- `yaml()`：在 `dict()` 基础上 dump 成 YAML 字符串，还能可选地与一个**已有的 recipe 文件**合并（deep merge），用来增量叠加 recipe。

此外，`filter_dict` 提供按 stage 过滤字典的能力——当一个 recipe 文件里写了多个 stage，但本次只想跑其中一个时使用。

#### 4.4.2 核心流程

**序列化（对象→字典）** 在 `get_yaml_serializable_dict` 里：

```
对每个 modifier:
    group = modifier.group 或 stage       # group 为空则回退到 stage 名
    组装 stage_dict[stage_name][group_name][类名] = {筛选过的参数}
其中参数筛选规则：
    去掉值为 None 的键
    去掉以 "_" 结尾的键（运行时私有状态标志）
    去掉 "group" 键本身（group 已体现在外层键名里）
```

**合并（两个 recipe 字典叠加）** 在 `append_recipe_dict` 里：当两个 recipe 有相同 stage 键（如都叫 `test_stage`）时，给它们加数字后缀 `test_stage_0`、`test_stage_1` 区分，避免互相覆盖。

#### 4.4.3 源码精读

[src/llmcompressor/recipe/utils.py:56-96](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/utils.py#L56-L96) —— `get_yaml_serializable_dict`。注意第 85-89 行的参数筛选：`not k.endswith("_")` 排除 `initialized_` 这类运行时标志，`k != "group"` 不把 group 当普通参数写出（因为它已被编码进外层 `group_name` 键）。

[src/llmcompressor/recipe/recipe.py:232-237](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L232-L237) —— `dict()` 就是直接转发给上面的工具函数。

[src/llmcompressor/recipe/recipe.py:239-282](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L239-L282) —— `yaml()`。第 254-257 行可选地读入 `existing_recipe_path` 指向的旧 recipe；第 266 行 `append_recipe_dict` 做合并；第 269-277 行用 `yaml.dump` 输出（`sort_keys=False` 保持插入顺序，`width=88` 控制换行）。

[src/llmcompressor/recipe/utils.py:99-109](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/utils.py#L99-L109) —— `filter_dict`：`target_stage` 为空时原样返回，否则只保留以 `target_stage` 开头的顶层键。

[src/llmcompressor/recipe/utils.py:112-139](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/utils.py#L112-L139) —— `append_recipe_dict`：键冲突时给双方都加 `_0` / `_1` 后缀（第 130-132 行），已带后缀的则继续递增找空位（第 133-138 行）。

#### 4.4.4 代码实践

1. **实践目标**：验证「解析→序列化→再解析」是往返无损的（round-trip）。
2. **操作步骤**：复现官方测试 `test_serialization` 的核心：
   ```python
   # 示例代码
   from llmcompressor.recipe import Recipe

   src = """
   test_stage:
       smoothquant_modifiers:
           SmoothQuantModifier:
               smoothing_strength: 0.5
   """
   r1 = Recipe.create_instance(src)
   dumped = r1.yaml()                 # 对象 → YAML 字符串
   r2 = Recipe.create_instance(dumped)  # YAML 字符串 → 对象

   print(r1.dict() == r2.dict())
   print("---dumped yaml---")
   print(dumped)
   ```
3. **需要观察的现象**：`r1.dict() == r2.dict()` 为 `True`；打印出的 YAML 仍是 `test_stage / smoothquant_modifiers / SmoothQuantModifier` 三层结构。
4. **预期结果**：`True`，且 dump 出的文本结构与原始 YAML 等价。
5. **待本地验证**：`SmoothQuantModifier` 若有更多必填字段（如 `mappings`），上面的最小 YAML 可能构造失败，需要补全字段。

#### 4.4.5 小练习与答案

**练习 1**：为什么序列化时要把以 `_` 结尾的字段（如 `initialized_`）排除掉？
**答案**：这些带下划线后缀的是 pydantic 运行时私有状态标志（u2-l3 讲过），不属于 recipe 配置。若写进 YAML，再读回来时会污染 modifier 的初始状态，甚至触发 `extra="forbid"` 校验错误。

**练习 2**：`append_recipe_dict` 为什么在键冲突时要给「原始那份」也加 `_0` 后缀，而不是只给新来的加？
**答案**：为了保证命名一致——只要发生冲突，所有同 base 名的 stage 都带数字后缀，下标从 0 连续递增，避免出现「一个无后缀、一个有后缀」的混乱状态。

---

### 4.5 顺序校验：AWQ 必须后接量化 modifier

#### 4.5.1 概念说明

有些压缩动作本身**不产生最终的量化权重**，它们只是对权重做一种「变换（transform）」，必须再跟一个真正的量化 modifier 才能完成压缩。AWQ 就是典型：它先为关键通道计算缩放因子并重排权重（变换），但真正把权重压成低位的，是排在它后面的 `QuantizationModifier`（或同样具备量化能力的 `GPTQModifier`）。

`Recipe` 用一个 pydantic `model_validator(mode="after")` 来强制这条规则：**一旦发现某个 AWQ 变换 modifier 后面没有跟任何量化 modifier，就抛 `ValueError`**。这条校验在对象构造完成（`create_instance` 返回前）自动触发，不用用户手动调用。

#### 4.5.2 核心流程

```
validate_model_after(model):   # model 是刚构造好的 Recipe
    if 没有 modifiers: 直接返回
    for 每个 modifier 及其下标 i:
        if modifier 是 AWQModifier(变换类):
            if 它后面(i+1 起)没有任何 QuantizationMixin 子类:
                raise ValueError("...AWQ must be run with ...")
    return model
```

判定「是不是量化类」靠 `isinstance(mod, QuantizationMixin)`——只要某个 modifier 继承了 `QuantizationMixin`，就被视为「能完成量化」，可作为 AWQ 的后续（`GPTQModifier`、`QuantizationModifier` 都是）。

#### 4.5.3 源码精读

[src/llmcompressor/recipe/recipe.py:206-230](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/recipe/recipe.py#L206-L230) —— `validate_model_after`。几处关键：

- 第 213-215 行：注释说明为何要 early return——全局压缩会话在 modifiers 能被 import 之前就已实例化，所以 `modifiers` 为空时必须提前返回，避免触发下面那两行 `from llmcompressor... import` 造成循环依赖。
- 第 217-218 行：延迟导入 `QuantizationMixin` 与 `AWQModifier`（从 `transform` 子包），同样是规避循环依赖。
- 第 220-224 行：核心判断——`AWQModifier` 之后是否存在 `QuantizationMixin`。
- 第 225-228 行：抛错（提示信息文本以 "AWQ must be run with " 结尾，是源码现状）。

这条校验的真实样例可在测试里看到：

[src/llmcompressor/recipe/test_recipe.py:109-192](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/recipe/test_recipe.py#L109-L192) —— `test_recipe_validate_after` 用参数化用例列出了多种组合：单独的变换类 `AWQTransformModifier`（非法）、变换 + `QuantizationModifier`（合法）、变换 + `GPTQModifier`（合法）、量化在前变换在后（非法，因为 AWQ 后面没有量化）。注意：单独的旧版 `AWQModifier`（函数垫片）是合法的，因为它本身返回的列表里已经自带一个 `QuantizationModifier`（见 4.2.3 引用的 `awq/__init__.py`）。

#### 4.5.4 代码实践

1. **实践目标**：亲手触发并理解「AWQ 缺后续量化」的报错。
2. **操作步骤**：
   ```python
   # 示例代码
   from llmcompressor.recipe import Recipe
   from llmcompressor.modifiers.transform import AWQModifier as AWQTransform
   from llmcompressor.modifiers.quantization import QuantizationModifier

   # 情况一：只给变换，缺量化 → 应当报错
   try:
       Recipe.create_instance([AWQTransform(duo_scaling="both")])
       print("情况一：未报错（异常！）")
   except ValueError as e:
       print("情况一：按预期报错 ->", str(e)[:60], "...")

   # 情况二：变换 + 量化 → 合法
   r = Recipe.create_instance([
       AWQTransform(duo_scaling="both"),
       QuantizationModifier(scheme="W4A16_ASYM", targets="Linear", ignore="lm_head"),
   ])
   print("情况二：合法，modifiers 数 =", len(r.modifiers))
   ```
3. **需要观察的现象**：情况一抛 `ValueError`，信息中含 "AWQ"；情况二正常返回，`modifiers` 长度为 2。
4. **预期结果**：与测试 `test_recipe_validate_after` 中 `is_valid=False/True` 的断言一致。
5. **待本地验证**：导入 `AWQTransform` 时若伴随 `DeprecationWarning`，可参考测试文件顶部用 `warnings.catch_warnings()` 抑制（见 [test_recipe.py:9-11](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/recipe/test_recipe.py#L9-L11)）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `[QuantizationModifier, AWQTransformModifier]`（量化在前、变换在后）是非法的？
**答案**：校验只检查 AWQ **之后**是否存在量化 modifier。变换在后时，它后面没有任何量化动作，重排后的权重不会被真正量化，所以非法。这也呼应 u1-l1 讲过的规则：变换类 modifier 必须排在量化类**之前**。

**练习 2**：这条顺序校验为什么用 `model_validator(mode="after")` 而不是在 `from_dict` 里手动检查？
**答案**：`mode="after"` 保证**无论 recipe 从哪条路径创建**（字符串、文件、对象列表），只要最终组装出 `Recipe` 对象，校验都会自动触发，覆盖面最全、不会漏判。而写在 `from_dict` 里只能覆盖文本路径，漏掉 `from_modifiers` 这条对象路径。

---

## 5. 综合实践

把本讲五个模块串起来，完成一个「手写 recipe → 解析 → 改造 → 序列化 → 再解析」的完整往返任务。

**任务背景**：你想给一个模型做「先 SmoothQuant 平滑激活、再 W8A8 量化」的 PTQ，但只想用 recipe 文本驱动，并且最后要把改造后的 recipe 存盘。

**步骤**：

1. 编写一段 YAML 字符串，包含一个 stage、一个 `quantization_modifiers` group，里面放 `QuantizationModifier(scheme="W8A8_INT", targets="Linear")`。参考 `tests/llmcompressor/helpers.py` 里 `valid_recipe_strings()` 提供的合法写法（[tests/llmcompressor/helpers.py:4-16](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/helpers.py#L4-L16) 给出了 SmoothQuant 的完整字段示例，含 `mappings`）。
2. 用 `Recipe.create_instance(yaml_str)` 解析，打印 `recipe.modifiers` 的类名与 `recipe.stage`。
3. 在内存里构造第二个 recipe：`Recipe.create_instance([SmoothQuantModifier(...)])`（字段参考 helpers 里的样例），然后调用 `recipe1.yaml(existing_recipe_path=<把 recipe2 先 yaml 存盘的路径>)`，观察 `append_recipe_dict` 给同 stage 加的 `_0/_1` 后缀。
4. 把合并后的 YAML 再用 `Recipe.create_instance` 解析回来，断言 `recipe.dict()` 往返一致。
5. （进阶）故意写一段只含 `AWQTransformModifier` 而无后续量化的 recipe，确认 `create_instance` 在校验阶段就报错，根本走不到 `oneshot`。

**预期现象与判据**：

- 第 2 步：`modifiers` 长度为 1，类型为 `QuantizationModifier`，`stage` 为你写的 stage 名。
- 第 3 步：合并后的 YAML 里出现形如 `<stage>_stage_0`、`<stage>_stage_1` 的键。
- 第 4 步：往返后的 `dict()` 与原 `dict()` 相等（参考 `test_serialization`）。
- 第 5 步：抛 `ValueError`。

> 若本地缺少某些 modifier 的必填字段，请以 `tests/llmcompressor/helpers.py` 与 `tests/llmcompressor/recipe/test_recipe.py` 中的字段为准——它们是「合法 recipe 长什么样」的最权威样例。运行具体数值结果**待本地验证**。

## 6. 本讲小结

- `Recipe` 是不执行压缩的「容器 + 翻译器」，三个字段 `args` / `stage` / `modifiers` 中，`modifiers` 才是最终被 lifecycle 驱动的真实对象。
- `create_instance` 是多源总入口，按 `Recipe → Modifier/list → 文件 → 字符串` 的顺序分派，把异构输入统一归一为 `Recipe`。
- `from_dict` 靠 `_stage` / `_modifiers` 后缀约定拆解三层结构，并用 `ModifierFactory.create` 把类名字符串实例化——这里闭环了 u2-l4 的工厂机制。
- 序列化走 `dict()` / `yaml()`，参数筛选会剔除 `None`、`_` 结尾的运行时标志和 `group` 字段；合并用 `append_recipe_dict` 给冲突 stage 加数字后缀；`filter_dict` 支持按 stage 过滤。
- 顺序校验由 `model_validator(mode="after")` 自动触发：AWQ 变换 modifier 之后必须存在 `QuantizationMixin` 子类，否则在对象构造阶段就报错。
- 解析→序列化→再解析是往返无损的，这是 recipe 可作为可存盘、可 diff 配置文件的基础。

## 7. 下一步学习建议

本讲把「文本 recipe → Modifier 对象列表」的链路讲完了。接下来：

- **进入第三单元（量化与校准管线）**：推荐先读 [u3-l1 QuantizationModifier 与量化方案]，看 `QuantizationModifier`（本讲反复出现的那个类）的 `on_initialize` / `on_calibration_start` 等钩子到底做了什么。
- **追全链路**：如果想看 recipe 在真实压缩流程中的位置，回看 [u1-l4] 的 `apply_recipe_modifiers`，它调用 `session.initialize(recipe=...)`，而 `lifecycle.initialize` 正是用 `Recipe.create_instance`（本讲核心）把传入的 recipe 变成对象、再逐个 `mod.initialize`。
- **扩展实践**：等学到 [u6-l4 自定义 Modifier] 后，可以把自己写的 modifier 用 `ModifierFactory.register` 登记进工厂，然后直接写进本讲的 YAML recipe 里使用，体会「文本类名 → 工厂 → 对象 → 生命周期」的完整闭环。
