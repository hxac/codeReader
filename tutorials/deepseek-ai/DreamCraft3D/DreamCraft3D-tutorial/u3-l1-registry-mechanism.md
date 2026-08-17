# 注册机制：@register 装饰器与 find 查找

## 1. 本讲目标

DreamCraft3D（threestudio 框架）有 41 个可替换组件：几何、材质、背景、渲染器、引导模型、提示词处理器、导出器、数据模块、系统……它们全部通过同一套机制接入框架。学完本讲，你应该能够：

1. 解释 `__modules__` 字典、`@threestudio.register` 装饰器与 `threestudio.find` 三者如何构成一个极简「插件总线」。
2. 说出 `import threestudio` 这一句代码如何通过包 `__init__.py` 链条把全部组件登记进注册表，并理解其中循环导入为何不会出错。
3. 跟踪配置 yaml 里的 `*_type` 字符串从被读取、到 `find`、再到实例化的完整消费链路。
4. 独立实现一个新组件（垂直渐变背景），用 `custom_import` 不改一行仓库源码就接入训练配置。

## 2. 前置知识

### 2.1 装饰器（decorator）

装饰器是「接收一个函数/类、返回一个函数/类」的语法糖。本讲遇到的是**带参数的装饰器工厂**：

```python
@threestudio.register("solid-color-background")   # 先调用 register("...") 得到 decorator
class SolidColorBackground(BaseBackground):        # 再用 decorator 装饰类
    ...
```

它等价于 `SolidColorBackground = threestudio.register("solid-color-background")(SolidColorBackground)`。两层调用：外层接收名字，内层接收类。

### 2.2 模块级全局状态与 import 的「副作用」

Python 中 `import module` 会**执行**该模块的顶层代码一次，并把模块对象缓存进 `sys.modules`。所以 `@register(...)` 这种写在模块顶层的装饰器，只有在模块被导入时才会真正运行——这就是「导入即注册」。

### 2.3 注册表模式（Registry Pattern）

注册表 = 一张全局字典 `{名字字符串: 类}`。它把「配置文件里的字符串」与「Python 类」解耦：yaml 不需要 import 任何东西，只写名字；框架拿到名字后查表取类。这是 Stable Diffusion WebUI、threestudio 等大量插件化项目的通用做法。

### 2.4 嵌套 dataclass 配置

每个可注册组件都在类内部定义一个 `Config` dataclass 描述可用参数（如 `color`、`learned`）。实例化时，配置 dict 会被 `OmegaConf.structured` 严格校验：**写了不存在的键会报错，漏写的键用默认值**。这一点决定了你给新组件加配置项时必须先声明字段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/__init__.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L1-L37) | 注册表本体：`__modules__` 字典、`register`、`find`，以及触发全量注册的那行 `from . import data, models, systems` |
| [threestudio/models/background/solid_color_background.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/solid_color_background.py#L1-L51) | 「被注册组件」的样板：如何声明注册名、Config、configure 与 forward |
| [launch.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L98-L122) | 注册表的消费方与 `custom_import` 扩展入口 |
| [threestudio/utils/config.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L56-L117) | `ExperimentConfig`（含 `custom_import` 字段）、`load_config`、`parse_structured` |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L286-L296) | `find` 的递归消费现场：system 内部再 find geometry/material/background/renderer |
| [threestudio/utils/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L89-L118) | `BaseModule.__init__`：被 find 出来的类实例化时统一走的标准流程 |
| [threestudio/data/image.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L313) 与 [threestudio/data/images.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/images.py#L313) | 同名注册的对照案例：一个生效、一个因未被导入而不生效 |

## 4. 核心概念与源码讲解

### 4.1 模块一：`__modules__` 字典——十行代码实现的插件总线

#### 4.1.1 概念说明

框架需要支持「在 yaml 里换一个字符串就换一套几何/渲染器/引导模型」。最简单的实现就是一张全局字典：组件作者把类登记进去（register），框架按名字取出来（find）。DreamCraft3D 的实现只有 13 行，没有任何魔法：没有排序、没有优先级、没有命名空间——**后写入者直接覆盖先写入者**，这是后面理解同名注册冲突的关键。

#### 4.1.2 核心流程

以注册 `solid-color-background` 为例，解释器执行顺序：

```text
1. 求值 @threestudio.register("solid-color-background")
   → 调用 register("solid-color-background")，name 被「闭包」捕获
   → 返回内层函数 decorator
2. Python 完成类体定义，得到 SolidColorBackground
3. 调用 decorator(SolidColorBackground)
   → 执行 __modules__["solid-color-background"] = SolidColorBackground   # 登记进全局字典
   → return cls（原样返回，装饰器不修改类）
4. 此后任何代码 threestudio.find("solid-color-background")
   → 返回 __modules__["solid-color-background"]，即拿到类本身
   → find(...)(cfg) 再调用类构造函数完成实例化
```

查找失败没有友好提示：名字不存在时 `__modules__[name]` 直接抛 `KeyError`。所以配置里 `*_type` 拼错时，你会看到的是一个裸的 `KeyError: 'xxx-type'`，而不是「找不到组件」之类的说明——这是排查配置错误时的重要线索。

#### 4.1.3 源码精读

注册表本体（整个机制的核心只有这几行）：

- [threestudio/__init__.py:L1-L13](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L1-L13) —— L1 定义模块级字典 `__modules__ = {}`，它随 `import threestudio` 创建、进程内全局唯一；L4-L9 的 `register(name)` 是装饰器工厂，内层 `decorator` 把类写入字典后原样返回；L12-L13 的 `find(name)` 就是一次字典取值。

被注册组件的样板（注册发生在类定义这一行）：

- [threestudio/models/background/solid_color_background.py:L13-L21](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/solid_color_background.py#L13-L21) —— L13 用 `@threestudio.register("solid-color-background")` 登记，注册名用的是 kebab-case（连字符小写），与 yaml 里 `background_type` 的取值完全一致；L15-L21 在类内声明嵌套 `Config` dataclass，继承自 `BaseBackground.Config`。

- [threestudio/models/background/base.py:L13-L23](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/base.py#L13-L23) —— `BaseBackground` 定义了背景组件的接口约定：实现 `configure()`（初始化参数/缓冲区）与 `forward(dirs)`（按射线方向输出 `B H W Nc` 颜色）。新组件只要满足这个接口，渲染器就能无差别地使用它。

补充说明：`__modules__` 以双下划线开头只是「内部使用」的命名约定，Python 并不真正禁止外部访问——事实上 `find` 自己就是直接访问它的，后面实践中我们也会直接打印它。

#### 4.1.4 代码实践

**实践：用纯 Python 复刻迷你注册表，观察装饰器时序与覆盖语义**（不需要任何依赖， anywhere 可跑）。

1. 实践目标：亲眼确认「register 在类定义瞬间执行」「find 只是查字典」「同名后写覆盖先写」三件事。
2. 操作步骤：新建 `mini_registry.py`（示例代码）：

```python
# 示例代码：mini_registry.py
__modules__ = {}

def register(name):
    def decorator(cls):
        print(f"[register] {name} -> {cls.__name__}")
        __modules__[name] = cls
        return cls
    return decorator

def find(name):
    return __modules__[name]

@register("my-plugin")
class PluginV1:
    pass

@register("my-plugin")   # 故意同名
class PluginV2:
    pass

print(find("my-plugin"))            # 应打印 PluginV2
try:
    find("not-exist")
except KeyError as e:
    print("KeyError:", e)
```

3. 需要观察的现象：两行 `[register]` 在**导入/执行时**立即打印（而不是调用 find 时）；`find("my-plugin")` 拿到的是 `PluginV2`。
4. 预期结果：`PluginV2` 与 `KeyError: 'not-exist'`，证明注册表是「后写者胜」的普通字典。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `register` 改成不带参数的直接装饰器 `@register`（直接修饰类），注册名从哪里来？有什么局限？

> **答案**：只能在 `decorator(cls)` 里用 `cls.__name__`（即类名）做键，写成 `__modules__[cls.__name__] = cls`。局限是注册名被迫与 Python 类名一致，无法使用 `"solid-color-background"` 这种带连字符的、与 yaml 风格统一的名称，也无法给同一个类注册多个别名。

**练习 2**：`find` 找不到名字时框架会给出什么信息？这提示你排查什么问题？

> **答案**：抛出裸 `KeyError: 'xxx'`。遇到它应优先检查：`*_type` 是否拼错、目标文件是否真的被导入（见模块二）、`custom_import` 是否配置正确（见模块四）。

**练习 3**：两个类注册了同一个名字，谁生效？

> **答案**：后执行装饰器的那个生效（字典赋值覆盖）。真实案例：`threestudio/data/image.py` 与 `threestudio/data/images.py` 都注册了 `"single-image-datamodule"`，但只有前者被导入，所以 images.py 的注册从未写入（详见模块二）。

### 4.2 模块二：导入即注册——包 `__init__.py` 链与循环导入的化解

#### 4.2.1 概念说明

装饰器只有在其所在模块**被执行**时才运行。DreamCraft3D 没有集中式的「组件清单文件」，而是把注册的触发藏在包的导入链里：每个子包的 `__init__.py` 只做一件事——`from . import` 本包的所有组件文件。于是 `import threestudio` 一句话就会像多米诺一样把 41 个组件全部登记。理解这条链，你才能回答两个高频问题：「我在 models/ 下新建了一个文件，为什么 find 不到？」「`import threestudio` 会不会和子包里的 `import threestudio` 循环导入崩溃？」

#### 4.2.2 核心流程

`launch.py` 执行 `import threestudio` 后的导入链（缩进表示触发方）：

```text
import threestudio                                  # launch.py L74
└─ threestudio/__init__.py L36: from . import data, models, systems
   ├─ data/__init__.py:      from . import image, uncond
   │    ├─ image.py  L313:  @register("single-image-datamodule")      ✅ 生效
   │    └─ uncond.py L470:  @register("random-camera-datamodule")     ✅ 生效
   │    （images.py 未出现在上面任何 import 里 → 其 L313 的同名注册 ✗ 不生效）
   ├─ systems/__init__.py:   from . import dreamcraft3d, zero123
   │    ├─ dreamcraft3d.py L21: @threestudio.register("dreamcraft3d-system")
   │    └─ zero123.py     L17: @threestudio.register("zero123-system")
   └─ models/__init__.py:    from . import (background, exporters, geometry,
                                            guidance, materials,
                                            prompt_processors, renderers)
        └─ models/background/__init__.py: from . import (base,
                neural_environment_map_background, solid_color_background,
                textured_background)
             └─ solid_color_background.py L13:
                  @threestudio.register("solid-color-background")     ← 真正写入字典的时刻
```

**循环导入为何不崩**：`solid_color_background.py` 顶层有 `import threestudio`，而 `threestudio/__init__.py` 又要导入它——这是教科书式的循环。它能工作的前提是**顺序**：`register`/`find` 定义在 [threestudio/__init__.py:L1-L13](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L1-L13)，而触发子包导入的 `from . import data, models, systems` 在 [L36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L36)。当子包文件执行 `import threestudio` 时，`sys.modules` 里已经有一个「初始化进行中」的 threestudio 模块，import 语句立即返回它——此时 `register` 属性已定义，装饰器得以正常执行。若把 L36 挪到文件第一行，子包执行 `@threestudio.register(...)` 时 `register` 尚未定义，程序会以 `AttributeError` 崩溃。

#### 4.2.3 源码精读

- [threestudio/__init__.py:L36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L36) —— 全部注册的总开关：`from . import data, models, systems`。这一行之前的 L17-L33 只是给日志着色准备的 `debug/info/warn` 快捷函数，与注册无关。
- [threestudio/models/__init__.py:L1-L8](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/__init__.py#L1-L8) —— 逐个导入七个孙包（background、exporters、geometry、guidance、materials、prompt_processors、renderers），对应三维流水线的七个可替换环节。
- [threestudio/models/background/__init__.py:L1-L6](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/__init__.py#L1-L6) —— 叶子级导入：四个背景文件在这里被逐个执行，装饰器随之写入注册表。
- [threestudio/data/image.py:L313](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L313) 与 [threestudio/data/images.py:L313](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/images.py#L313) —— 两个文件都在同名行号处用 `@register("single-image-datamodule")` 注册（注意这里是从 `threestudio import register` 的写法）。区别在于 [threestudio/data/__init__.py:L1](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/__init__.py#L1) 只写了 `from . import image, uncond`，images.py 从未被链上任何 import 触达，所以它的注册是「死代码」；若手动导入它，则会按模块一的「后写者胜」规则覆盖 image.py 的注册。

按导入链统计，`import threestudio` 后注册表共 **41 个**组件：

| 目录 | 生效注册数 | 代表注册名 |
| --- | --- | --- |
| models/guidance | 11 | `deep-floyd-guidance`、`stable-diffusion-bsd-guidance`、`stable-zero123-guidance` |
| models/materials | 6 | `no-material`、`diffuse-with-point-light-material` |
| models/geometry | 5 | `implicit-volume`、`implicit-sdf`、`tetrahedra-sdf-grid` |
| models/renderers | 5 | `nerf-volume-renderer`、`nvdiff-rasterizer` |
| models/prompt_processors | 4 | `deep-floyd-prompt-processor` |
| models/background | 3 | `solid-color-background`、`textured-background` |
| data | 2 | `single-image-datamodule` |
| systems | 2 | `dreamcraft3d-system` |
| models/exporters | 2 | `mesh-exporter` |
| utils/perceptual | 1 | `perceptual-loss` |

#### 4.2.4 代码实践

**实践：验证「import 即注册」与未导入文件的不生效**（只需能 `import threestudio`，不加载任何模型权重；由于 `import threestudio` 会连带导入 torch 相关依赖，需在完成 u1-l2 环境安装后进行）。

1. 实践目标：确认注册表在 `import threestudio` 后自动填满，且 `data/images.py` 的重名注册不在其中。
2. 操作步骤（示例命令）：

```bash
# 步骤 1：在仓库根目录执行
python -c "import threestudio; print(len(threestudio.__modules__))"
# 步骤 2：列出全部注册名
python -c "import threestudio; print(sorted(threestudio.__modules__))"
# 步骤 3：确认 images.py 未被导入
python -c "import sys, threestudio; print('threestudio.data.images' in sys.modules)"
# 步骤 4：手动导入 images.py，观察覆盖
python -c "
import threestudio
before = threestudio.__modules__['single-image-datamodule']
import threestudio.data.images   # 手动触发其装饰器
after = threestudio.__modules__['single-image-datamodule']
print(before is after, before, after)
"
```

3. 需要观察的现象：步骤 1 打印的数字；步骤 3 打印 `False`；步骤 4 中两个类对象不同（`before is after` 为 `False`），证明导入 images.py 后注册被覆盖。
4. 预期结果：步骤 1 应为 `41`（与本讲统计表一致；若上游代码更新导致数量变化，以你本地输出为准）。步骤 4 打印 `False`。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：在 `threestudio/models/guidance/` 下新建 `my_guidance.py` 并写好装饰器，但没改任何 `__init__.py`，`find("my-guidance")` 能成功吗？

> **答案**：不能。装饰器未执行，名字不在 `__modules__` 里，抛 `KeyError`。解决办法有二：把文件加入对应 `__init__.py` 的 import 列表（侵入仓库），或用模块四的 `custom_import`（不侵入仓库，推荐）。

**练习 2**：为什么 `threestudio/__init__.py` 把 `from . import data, models, systems` 放在文件末尾而不是开头？

> **答案**：子包文件顶层都有 `import threestudio` 并在模块级使用 `@threestudio.register(...)`。放在开头会使子包在 `register` 定义之前执行，访问部分初始化的 threestudio 模块时报 `AttributeError`。放在末尾保证了「先定义工具、再触发子包」的安全顺序。

### 4.3 模块三：find 的消费现场——从 `*_type` 字符串到可训练对象

#### 4.3.1 概念说明

注册表的价值在于消费。DreamCraft3D 的约定是：**配置里每个 `X_type` 键的值是一个注册名，同名兄弟段 `X` 是传给该类的参数 dict**。例如 coarse-nerf 配置的 `background_type: "solid-color-background"` 决定用哪个背景类。`find` 返回的是**类**，紧随其后的 `(...)` 才是实例化；所有组件类都继承 `BaseModule`，其构造函数统一完成「参数校验 → configure → 可选权重加载」三步，所以 `find(X_type)(cfg.X)` 这一行的产出就是一个配置齐全、可直接训练/调用的对象。

#### 4.3.2 核心流程

`find` 的消费是**递归**的——system 自己被 find 出来，又在内部 find 别人：

```text
launch.py main()
 ├─ cfg = load_config(...)                                   # yaml+CLI 合并出 ExperimentConfig
 ├─ dm = threestudio.find(cfg.data_type)(cfg.data)           # 第一层：数据模块
 └─ system = threestudio.find(cfg.system_type)(cfg.system, resumed=...)   # 第一层：系统
     └─ BaseLift3DSystem.configure()                          # 第二层（systems/base.py）
         ├─ geometry  = find(cfg.geometry_type)(cfg.geometry)
         ├─ material  = find(cfg.material_type)(cfg.material)
         ├─ background= find(cfg.background_type)(cfg.background)
         └─ renderer  = find(cfg.renderer_type)(cfg.renderer, geometry=…, material=…, background=…)
     └─ ImageConditionDreamFusion.configure()                 # 第三层（dreamcraft3d.py）
         ├─ guidance         = find(cfg.guidance_type)(cfg.guidance)
         ├─ guidance_3d      = find(cfg.guidance_3d_type)(…)   # texture 阶段为 None 则跳过
         ├─ prompt_processor = find(cfg.prompt_processor_type)(…)
         ├─ perceptual_loss  = find("perceptual-loss")(…)      # 甚至硬编码名字直接查表
         └─ control_guidance = find(cfg.control_guidance_type)(…)
```

实例化时（`BaseModule.__init__`）：`parse_structured(self.Config, cfg)` 把 dict 校验并转成结构化配置 → `self.configure()` 建立参数与缓冲区 → 若 `cfg.weights` 非空则加载检查点。配置 dict 里出现 `Config` 未声明的键会在第一步就被 `OmegaConf.structured` 拒绝。

#### 4.3.3 源码精读

- [launch.py:L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L109) —— `dm = threestudio.find(cfg.data_type)(cfg.data)`：注册机制最直白的消费现场，`find` 取类、`(cfg.data)` 传参实例化。
- [launch.py:L120-L122](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L120-L122) —— 用 `find(cfg.system_type)` 构建 system；注意 system 除了 `cfg.system` 还接收 `resumed` 关键字，说明被 find 的类可以在标准流程外附加自己的构造参数。
- [threestudio/systems/base.py:L286-L296](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L286-L296) —— 第二层消费：geometry、material、background、renderer 四连 `find`。L289-L291 正是背景的接入点：`self.background = threestudio.find(self.cfg.background_type)(self.cfg.background)`。你换掉 `background_type` 字符串，被实例化的就是这里换的类。
- [threestudio/systems/dreamcraft3d.py:L43-L60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L43-L60) —— 第三层消费：guidance、guidance_3d、prompt_processor、perceptual_loss（L56 直接硬编码字符串 `"perceptual-loss"` 查表）与可选的 control_guidance，全部走同一套 find。
- [threestudio/utils/base.py:L96-L102](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L96-L102) —— `BaseModule.__init__` 的标准三步：L100 `parse_structured(self.Config, cfg)` 校验参数，L102 调用 `self.configure()` 初始化，L103-L112 处理可选的 `weights` 热启动。任何被 find 出来的组件实例化时都经过这里。
- [threestudio/utils/config.py:L129-L131](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L129-L131) —— `parse_structured` 的实现仅一行 `OmegaConf.structured(fields(**cfg))`：以组件声明的嵌套 `Config` dataclass 为模板做严格校验，多余键报错、缺失键取默认值。
- [threestudio/models/background/solid_color_background.py:L25-L50](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/background/solid_color_background.py#L25-L50) —— 实例化之后发生什么：`configure`（L25-L34）把 `cfg.color` 存成 buffer（`learned=True` 时改为可训练 `nn.Parameter`）；`forward`（L36-L50）忽略方向 `dirs`，输出常量颜色，其中 L40-L50 的 `random_aug` 分支以 `random_aug_prob` 概率换成随机颜色——`color * 0 +` 的写法是刻意保留对 `env_color` 的依赖，防止 DDP 判定该参数「未使用」。这段与注册无关，但它是你仿写新背景的模板。

#### 4.3.4 代码实践

**实践：脱离 launch.py，手动走完「find → 实例化 → forward」**（只需 CPU 与 torch，不加载扩散模型权重）。

1. 实践目标：证明任何注册组件都可以独立于训练循环被取出并使用；同时体验 `Config` 的严格校验。
2. 操作步骤：在仓库根目录新建 `probe_background.py`（示例代码）并运行：

```python
# 示例代码：probe_background.py
import torch
import threestudio   # 触发全部注册

cls = threestudio.find("solid-color-background")     # 取类
print("找到类:", cls)

bg = cls({"color": (1.0, 0.0, 0.0), "learned": False})  # 传 dict 实例化
dirs = torch.zeros(2, 8, 8, 3)                        # 假的射线方向张量
out = bg(dirs)                                        # B H W 3
print("输出形状:", out.shape)
print("左上像素颜色:", out[0, 0, 0].tolist())          # 预期 [1.0, 0.0, 0.0]

# 体验严格校验：故意传一个不存在的键
try:
    cls({"not_a_key": 1})
except Exception as e:
    print("非法键被拒绝:", type(e).__name__)
```

3. 需要观察的现象：`find` 拿到的是 `SolidColorBackground` 类本身；forward 输出形状 `(2, 8, 8, 3)`；颜色为所配置的红色；传入未声明键时抛出 OmegaConf 的结构化配置错误。
4. 预期结果：依次打印类名、形状、`[1.0, 0.0, 0.0]` 与一个异常类型名（具体异常类型随 OmegaConf 版本可能是 `ConfigKeyError` 或 `ValidationError`）。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：为什么给 background 传 `{"weights": "path:module_name"}` 也能被接受，尽管 `SolidColorBackground.Config` 没有声明这个字段？

> **答案**：`SolidColorBackground.Config` 继承 `BaseBackground.Config`，后者又继承 `BaseModule.Config`（见 [threestudio/utils/base.py:L89-L92](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L89-L92)），链上有 `weights: Optional[str] = None`。所有 `BaseModule` 子类的配置都自动获得该字段，这是阶段间权重接力的通用入口。

**练习 2**：`threestudio.find(cfg.background_type)(cfg.background)` 中两个括号各做什么？

> **答案**：第一对括号调用 `find`，按键从 `__modules__` 取出**类**；第二对括号调用该类的构造函数（走 `BaseModule.__init__` 的参数校验与 `configure`），得到**实例**。

**练习 3**：如果把 yaml 中 `background_type` 拼成 `"solid_color_background"`（下划线），会在哪一行、以什么方式失败？

> **答案**：拼错不会在配置加载时报错，而是延迟到 [threestudio/systems/base.py:L289](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L289) 执行 `find` 时抛出 `KeyError: 'solid_color_background'`。

### 4.4 模块四：custom_import——不改动仓库源码的扩展注入

#### 4.4.1 概念说明

模块二告诉我们：放进仓库的组件文件必须改 `__init__.py` 才能生效。但直接改仓库有三个缺点：污染源码、难以跟随上游更新、无法把私有组件分发给别人。`custom_import` 解决了这个问题：它是 `ExperimentConfig` 的一个顶层字段（一个模块路径列表），`launch.py` 在构建任何组件**之前**逐个 `importlib.import_module` 它们——于是你的扩展文件哪怕放在仓库外的一个独立目录里，其顶层的 `@register` 也会在第一次 `find` 前执行完毕。这正是官方推荐的二次开发姿势。

#### 4.4.2 核心流程

```text
load_config(...)                                     # launch.py L100：cfg.custom_import 已就绪
if len(cfg.custom_import) > 0:                       # L102
    for extension in cfg.custom_import:              # L104
        importlib.import_module(extension)           # L105 → 执行扩展模块顶层代码
                                                     #        → @register(...) 写入 __modules__
dm = threestudio.find(cfg.data_type)(cfg.data)       # L109：此后所有 find 都能看到新注册
```

时机是关键：注入发生在**所有** find 之前，所以扩展可以注册全新的组件，也可以（借助模块一的「后写者胜」）覆盖仓库内置组件。模块路径必须是当前 Python 环境可导入的：在仓库根目录运行 `launch.py` 时，根目录及打包过的包都能被找到；放在别处的目录可用 `PYTHONPATH=/path/to/dir` 补充。

#### 4.4.3 源码精读

- [launch.py:L102-L105](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L102-L105) —— custom_import 的全部实现：循环调用 `importlib.import_module`，共四行。L103 的 `print(cfg.custom_import)` 会在训练日志开头打印扩展列表，可当作「注入是否生效」的直接证据。
- [threestudio/utils/config.py:L61-L62](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L61-L62) —— 字段定义 `custom_import: Tuple[str] = ()`，默认空元组表示不注入任何扩展。它是顶层字段，因此既能在 yaml 里写列表，也能用 `--custom_import '[...]'` 从命令行覆盖（`OmegaConf.from_cli` 按 YAML 语法解析值，列表需带方括号）。

#### 4.4.4 代码实践

**实践：最小 custom_import 空转测试**（先跑通注入机制本身，再进入综合实践写真正的组件）。

1. 实践目标：确认 `custom_import` 声明的模块会在任何 `find` 之前被导入。
2. 操作步骤：

```bash
# a. 在仓库根目录建 extensions/hello_ext.py，内容一行（示例代码）：
#    import threestudio; print(">>> hello_ext imported, modules =", len(threestudio.__modules__))
# b. 用 -c 直接模拟 launch.py 的注入顺序
python -c "
import importlib
for ext in ['extensions.hello_ext']:
    importlib.import_module(ext)
"
```

随后（可选，需要完整训练环境）任选一份配置副本，在顶层加两行后用 `--train` 启动，观察日志第一行是否出现 `('extensions.hello_ext',)` 与 `>>> hello_ext imported ...`。

3. 需要观察的现象：`importlib.import_module` 执行瞬间打印扩展模块的顶层输出，且打印发生在数据/系统构建之前。
4. 预期结果：控制台出现 `>>> hello_ext imported, modules = 41`；若把模块名改错，则在任何组件构建前抛 `ModuleNotFoundError`。（待本地验证）

#### 4.4.5 小练习与答案

**练习 1**：`custom_import` 里写了一个不存在的模块名，训练会怎样？

> **答案**：`importlib.import_module` 在 [launch.py:L105](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L105) 抛 `ModuleNotFoundError`，进程直接退出——因为注入先于一切 find，失败得非常早，报错栈也容易定位。

**练习 2**：能否用 `custom_import` 让自己的实现**替换**内置组件（例如自定义版 random-camera-datamodule）？

> **答案**：可以。注册表是后写者胜：把扩展模块放在 custom_import 列表里，其顶层 `@register("random-camera-datamodule")` 会在内置注册（import threestudio 时已完成）之后执行并覆盖同名条目，配置无需改动即可生效。

**练习 3**：`custom_import` 与在 `threestudio/models/background/__init__.py` 里加 import，两种方式的主要差别是什么？

> **答案**：后者直接修改仓库源码，升级仓库时会冲突，且组件必须放在 threestudio 包内；前者把扩展完全外置（独立目录甚至独立仓库），只需在配置里声明模块路径，仓库保持干净——这也是本讲综合实践采用的方式。

## 5. 综合实践：实现并接入 vertical-gradient-background（垂直渐变背景）

把四个模块串起来：**写组件（4.1 的注册样板）→ 靠导入生效（4.2/4.4 的 custom_import）→ 被系统 find 消费（4.3 的消费链）**。目标背景：底白顶蓝，按射线方向的竖直分量线性过渡，插值为

\[ c(t) = (1 - t) \cdot c_{\text{bottom}} + t \cdot c_{\text{top}}, \qquad t = \frac{\mathrm{clip}(d_y,\,-1,\,1) + 1}{2} \]

**步骤 1：创建扩展文件** `extensions/vertical_gradient_background.py`（示例代码，完全仿照 solid_color_background 的结构）：

```python
# 示例代码：extensions/vertical_gradient_background.py
from dataclasses import dataclass

import torch

import threestudio
from threestudio.models.background.base import BaseBackground
from threestudio.utils.typing import *


@threestudio.register("vertical-gradient-background")
class VerticalGradientBackground(BaseBackground):
    @dataclass
    class Config(BaseBackground.Config):
        n_output_dims: int = 3
        color_top: Tuple = (0.2, 0.3, 0.8)
        color_bottom: Tuple = (1.0, 1.0, 1.0)

    cfg: Config

    def configure(self) -> None:
        self.register_buffer(
            "color_top", torch.as_tensor(self.cfg.color_top, dtype=torch.float32)
        )
        self.register_buffer(
            "color_bottom", torch.as_tensor(self.cfg.color_bottom, dtype=torch.float32)
        )

    def forward(self, dirs: Float[Tensor, "B H W 3"]) -> Float[Tensor, "B H W Nc"]:
        # dirs 是每个像素的射线方向（世界坐标，y 轴向上）
        t = ((dirs[..., 1] + 1.0) / 2.0).clamp(0.0, 1.0).unsqueeze(-1)  # B H W 1
        color = self.color_bottom.to(dirs) * (1 - t) + self.color_top.to(dirs) * t
        return color
```

注意三个与源码对齐的细节：注册名用 kebab-case；`Config` 继承 `BaseBackground.Config`；`configure` 里用 `register_buffer` 而非普通属性，保证 `.to(device)` 时颜色随模型搬运。

**步骤 2：离线验证**（CPU 即可）。在仓库根目录运行：

```bash
python -c "
import torch, threestudio, extensions.vertical_gradient_background
cls = threestudio.find('vertical-gradient-background')
bg = cls({'color_top': (0.0, 0.0, 1.0), 'color_bottom': (1.0, 1.0, 1.0)})
dirs = torch.zeros(1, 64, 64, 3)
dirs[..., 1] = torch.linspace(-1, 1, 64).view(1, 64, 1)   # 底行 dy=-1，顶行 dy=+1
out = bg(dirs)
print(out.shape, out[0, 0, 0].tolist(), out[0, -1, 0].tolist())
"
```

预期输出形状 `(1, 64, 64, 3)`，底行像素接近 `[1.0, 1.0, 1.0]`（白），顶行接近 `[0.0, 0.0, 1.0]`（蓝）。（待本地验证）

**步骤 3：制作配置副本**。复制 `configs/dreamcraft3d-coarse-nerf.yaml` 为 `configs/my-coarse-gradient.yaml`，修改三处：

1. `name: "my-coarse-gradient"`（避免与原试验目录混淆，也避开自动续训逻辑）；
2. 顶层（与 `data_type` 同级）增加：

```yaml
custom_import:
  - extensions.vertical_gradient_background
```

3. 把 `background_type: "solid-color-background"` 改为 `background_type: "vertical-gradient-background"`（该键位于 `system:` 段内，见 [configs/dreamcraft3d-coarse-nerf.yaml:L79](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L79)）。渐变的两个颜色参数走默认值即可；如需覆盖，可在 yaml 的 `system.background` 段显式写 `color_top`/`color_bottom`（注意四份官方配置原本没有 `background:` 段，全部使用默认白色）。

命令行等价写法（不改 yaml 副本时，需 OmegaConf 按 YAML 解析列表与元组，推荐优先用 yaml 方式）：

```bash
python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
  --custom_import "['extensions.vertical_gradient_background']" \
  --system.background_type vertical-gradient-background
```

**步骤 4：训练验证**（需要 ≥20GB 显存与 u1-l2 的全部权重，属于完整验证）。用副本配置缩短步数试跑：

```bash
python launch.py --config configs/my-coarse-gradient.yaml --train --gpu 0 \
  --system.prompt_processor.prompt "A delicious hamburger" \
  --trainer.max_steps 50 --tag test-gradient
```

1. 需要观察的现象：启动日志开头先打印 `('extensions.vertical_gradient_background',)`（[launch.py:L103](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L103) 的 print），随后训练正常进入 step 循环；试验目录 `save/` 下的验证渲染图中，物体以外的背景由纯白变为上蓝下白的渐变。
2. 预期结果：50 步内背景即呈现渐变（背景不依赖训练，forward 直接输出）；几何仍是噪声球（步数太少），这不影响验证目标。（待本地验证）
3. 常见失败对照：日志出现 `KeyError: 'vertical-gradient-background'` → custom_import 未生效（检查模块路径与运行目录）；实例化时报结构化配置错误 → yaml 里 background 段的键名与 `Config` 字段不符。

## 6. 本讲小结

- 整个插件化架构只靠一张模块级字典 `__modules__`：`@threestudio.register("名")` 在类定义瞬间写入，`threestudio.find("名")` 就是查字典，未注册抛 `KeyError`，同名后写者胜。
- 注册靠 import 副作用触发：`threestudio/__init__.py` 末尾的 `from . import data, models, systems` 沿各级 `__init__.py` 层层下钻，一句 `import threestudio` 登记 41 个组件；`data/images.py` 因不在导入链上，其同名注册是死代码。
- 子包里 `import threestudio` 的循环导入能工作，靠的是「先定义 register/find、再导入子包」的顺序。
- 消费约定：配置里 `X_type` 的值是注册名、兄弟段 `X` 是参数；`find(X_type)(cfg.X)` 经 `BaseModule.__init__`（`parse_structured` → `configure` → 可选权重加载）产出实例，system 内部还会递归 find 四类组件与引导族。
- `custom_import` 让扩展完全外置：`launch.py` 在一切 find 之前 `importlib.import_module` 声明的模块，扩展既可新增也可覆盖内置注册，仓库源码零改动。

## 7. 下一步学习建议

本讲你已能「注册并接入」一个组件，但还没追问组件实例化之后的**生命周期**：`configure` 何时被调、`update_step` 如何随训练步数被驱动、渐进式训练（分辨率爬坡、哈希编码层级解锁）为何离不开它。这正是下一讲 **u3-l2「BaseModule、Configurable 与 Updateable 生命周期」** 的主题，建议重点预读 [threestudio/utils/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/base.py#L1-L118) 中 `Updateable.do_update_step` 的递归遍历，并思考：你刚写的渐变背景如果要让颜色随 `global_step` 变化，应该覆写哪个方法。
