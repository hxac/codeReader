# u1-l3 仓库目录结构与代码地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 DreamCraft3D 仓库根目录下每个文件/文件夹（`launch.py`、`gradio_app.py`、`preprocess_image.py`、`configs/`、`threestudio/`、`extern/`、`load/`）的职责。
2. 定位 `threestudio` 主包中 `systems`、`models`、`data`、`utils`、`scripts` 五大子包，并说出每个子包管什么。
3. 精读 [threestudio/__init__.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py)，理解 `@threestudio.register` 装饰器与 `threestudio.find` 查找这一对注册机制的实现。
4. 亲手统计项目中所有注册组件的分布，整理出一张「注册名 → 文件」对照表，并理解 `configs/dreamcraft3d-coarse-nerf.yaml` 里的每个 `*_type` 键正是通过注册名找到对应 Python 类的。

承接上一讲（u1-l1）：我们已经知道 DreamCraft3D 分四个阶段、每阶段一份 yaml。本讲要回答的问题是——**这些 yaml 里写的字符串（如 `implicit-volume`、`deep-floyd-guidance`）究竟对应仓库里的哪个文件？** 答案就是本讲的注册机制。

## 2. 前置知识

- **Python 装饰器**：`@decorator` 写在类或函数上方，本质是「先定义类，再把它传给装饰器函数，用返回值替换原名字」。本讲的注册机制就是一个把类存进字典的装饰器。
- **Python 包的 `__init__.py`**：一个目录带上 `__init__.py` 就是包；`import` 包时会先执行这个文件。DreamCraft3D 正是利用这一点，在「包被导入」的瞬间完成所有组件的注册。
- **字符串 → 类的映射**：yaml 配置里只能写字符串，而 Python 需要类来实例化对象。注册表（一个字典）就是两者之间的桥梁：装饰器负责「写入」（字符串键 → 类），`find` 负责「读取」。
- **PyTorch Lightning（可选了解）**：一个训练循环框架，`launch.py` 用它来组装 Trainer。本讲只需要知道它是训练的「发动机」即可，细节留到 u1-l4。

## 3. 本讲源码地图

| 文件/目录 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md) | 项目说明、安装、四阶段运行命令 | 命令中出现的关键路径 |
| [threestudio/__init__.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py) | 注册表定义 + 触发全包导入 | **本讲最小模块**，全文仅 36 行 |
| [launch.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py) | 训练/测试/导出统一入口 | `find(cfg.data_type)`、`find(cfg.system_type)` 两处调用 |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 粗阶段（NeRF）配置 | 所有 `*_type` 键的取值 |
| `threestudio/models/__init__.py`、`threestudio/systems/__init__.py`、`threestudio/data/__init__.py` | 各子包的导入清单 | 注册的触发链路 |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层结构：根目录的「七大成员」

#### 4.1.1 概念说明

DreamCraft3D 的仓库根目录非常干净：三个 Python 入口脚本 + 四个资源/代码目录。理解这七个成员，就拿到了整个项目的「门牌号」：

| 成员 | 类型 | 职责 |
| --- | --- | --- |
| `launch.py` | 入口脚本 | 统一训练入口：读配置 → 组装 PyTorch Lightning Trainer → 训练/验证/测试/导出 |
| `gradio_app.py` | 入口脚本 | 网页交互界面，内部以子进程方式调用 `launch.py` |
| `preprocess_image.py` | 入口脚本 | 输入图像预处理：去背景、生成深度图/法向图（u2-l1 精讲） |
| `metric_utils.py` | 工具脚本 | 评测指标计算（CLIP 相似度、PSNR、LPIPS 等） |
| `configs/` | 配置目录 | 四份阶段配置 yaml，是四阶段流水线的「总谱」 |
| `threestudio/` | 主代码包 | 全部核心逻辑：系统、模型、数据、工具、脚本 |
| `extern/` | 外部代码 | 第三方/外部模型的封装，如 `zero123.py` 与 `ldm_zero123/`（Zero123 的 LDM 实现） |
| `load/` | 资源目录 | 预训练权重、四面体网格、HDR 光照、示例图片 |
| `assets/`、`docs/`、`docker/`、`.github/` | 辅助目录 | README 图片、安装文档、Dockerfile、CI 工作流 |

#### 4.1.2 核心流程

一次完整的四阶段训练，数据在目录间的流动大致是：

```text
原图 ──preprocess_image.py──> load/images/*_rgba.png（RGBA 参考图）
                                    │
configs/dreamcraft3d-*.yaml ──> launch.py ──> threestudio/（训练逻辑）
                                    │              │
                                    │              ├── 引用 extern/（Zero123 等 LDM 代码）
                                    │              └── 读取 load/（预训练权重）
                                    ▼
                          outputs/<name>/<tag>/（检查点、日志、导出网格）
```

#### 4.1.3 源码精读

README 的 Quickstart 一节把这条流水线写成了四条命令，每条命令恰好对应一个入口 + 一份配置：

- [README.md:103-106](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L103-L106)：预处理命令 `python preprocess_image.py /path/to/image.png --recenter`，产出训练用的 RGBA 图。
- [README.md:112-126](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L112-L126)：四阶段命令。注意两点：配置文件按阶段切换（`dreamcraft3d-coarse-nerf.yaml` → `coarse-neus` → `geometry` → `texture`），而上一阶段的 `ckpts/last.ckpt` 通过 `system.weights=` 或 `system.geometry_convert_from=` 传给下一阶段——这是检查点在四个配置间接力的方式。
- [README.md:175](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L175)：导出网格命令，用 `system.exporter_type=mesh-exporter` 覆盖配置——注意这里又出现了一个 `*_type` 键，导出器也是注册组件。

`load/` 的内容（在 u1-l2 已详细讲过下载方式）按用途分为：`zero123/`（Zero123 权重与结构配置）、`tets/`（DMTet 四面体网格，如 `32_tets.npz`）、`lights/`（网格导出用的 HDR 环境光照）、`images/`（自带深度/法向三件套的示例图片，如 `mushroom_log_rgba.png`）。

#### 4.1.4 代码实践

1. **实践目标**：不借助任何工具，仅用 `ls` 建立「目录 → 职责」的直觉。
2. **操作步骤**：
   ```bash
   ls                    # 看根目录
   ls configs load       # 看配置与资源
   ls extern             # 看外部代码
   ```
3. **需要观察的现象**：`configs/` 里恰好只有四份 `dreamcraft3d-*.yaml`；`load/images/` 里每个示例都是 `_rgba/_depth/_normal` 三张成套出现。
4. **预期结果**：能口头回答「我要改训练超参去哪、要换预训练权重去哪、要找 Zero123 的 UNet 代码去哪」。
5. 运行结果待本地验证（本讲实践以阅读为主）。

#### 4.1.5 小练习与答案

**练习 1**：我想给一张自己的图片跑完整流水线，第一步应该把图片放在哪里、运行什么命令？

> **答案**：先运行 `python preprocess_image.py /path/to/image.png --recenter` 得到 RGBA 参考图（以及深度/法向图），训练时通过 `data.image_path=` 指向该 RGBA 图即可；图片不必强制放进 `load/images/`，但放在那里是最常见的约定（README Quickstart 用的是 `load/images/mushroom_log_rgba.png`）。

**练习 2**：`extern/` 和 `load/` 都和「预训练模型」有关，区别是什么？

> **答案**：`extern/` 放的是**代码**——外部模型的 Python 实现（如 Zero123 的 LDM 网络定义）；`load/` 放的是**数据资产**——权重文件（`.ckpt`）、四面体 `.npz`、HDR 光照、示例图。代码在 `extern/`，权重在 `load/`。

### 4.2 threestudio 主包：五大子包的职责分工

#### 4.2.1 概念说明

`threestudio/` 是主代码包（项目基于 threestudio 框架构建，README Credits 一节有说明）。它内部再分五个子包：

| 子包 | 职责 | 典型内容 |
| --- | --- | --- |
| `systems/` | 训练系统（PyTorch Lightning 模块），编排损失与训练步 | `dreamcraft3d.py`（核心系统）、`base.py`（基类） |
| `models/` | 可插拔的三维/二维模型组件 | 又分 7 个孙包：`geometry`、`materials`、`background`、`renderers`、`guidance`、`prompt_processors`、`exporters` |
| `data/` | 数据模块（相机与图像采样） | `image.py`（单图数据）、`uncond.py`（随机相机） |
| `utils/` | 通用工具 | `config.py`（配置）、`base.py`（基类）、`callbacks.py`、`misc.py`，以及 `perceptual/`（感知损失）等 |
| `scripts/` | 独立运行的脚本 | `img_to_mv.py`、`train_dreambooth_lora.py` 等 |

`models/` 的七个孙包正好对应三维生成流水线的七个可替换环节，值得单独记一张表：

| 孙包 | 管什么 | DreamCraft3D 流水线中用到（举例） |
| --- | --- | --- |
| `geometry/` | 三维几何表示 | `implicit-volume`（NeRF 粗阶段）、`tetrahedra-sdf-grid`（DMTet） |
| `materials/` | 材质/着色 | `no-material`（直接输出 RGB） |
| `background/` | 背景建模 | `solid-color-background` |
| `renderers/` | 可微渲染器 | `nerf-volume-renderer`、`nvdiff-rasterizer` |
| `guidance/` | 2D 扩散先验（引导模型） | `deep-floyd-guidance`、`stable-zero123-guidance`、`stable-diffusion-bsd-guidance` |
| `prompt_processors/` | 文本提示编码 | `deep-floyd-prompt-processor` |
| `exporters/` | 结果导出 | `mesh-exporter`（导出 obj） |

#### 4.2.2 核心流程

一个 `system`（系统）在 `configure` 阶段会按配置里的 `*_type` 把 `models/` 里的组件逐一实例化并组装起来（u6-l1 精讲）；`data/` 提供每个训练步的相机参数与参考图；`utils/` 提供配置解析、回调、损失工具；`scripts/` 则是游离于训练主循环之外的一次性工具（如生成多视图数据）。

```text
launch.py
   └── systems/dreamcraft3d.py（总指挥）
          ├── models/geometry    ┐
          ├── models/materials   │ 按 *_type 从注册表取出类并实例化
          ├── models/renderers   │
          ├── models/guidance    ┘
          └── data/image.py（每步喂数据）
```

#### 4.2.3 源码精读

- [threestudio/models/__init__.py:L1-L9](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/__init__.py#L1-L9)：`models` 子包的导入清单——导入 `background, exporters, geometry, guidance, materials, prompt_processors, renderers` 七个孙包。**这一行 import 的副作用就是注册所有模型组件**（机制见 4.3）。
- [threestudio/systems/__init__.py:L1](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/__init__.py#L1)：只导入 `dreamcraft3d, zero123` 两个系统。
- [threestudio/data/__init__.py:L1](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/__init__.py#L1)：只导入 `image, uncond` 两个数据模块。注意 `data/images.py` **不在**导入清单里——它里面也有一个 `single-image-datamodule` 注册（[threestudio/data/images.py:L313](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/images.py#L313)），与 `image.py` 的注册重名；由于从未被导入，它不会生效。这是阅读老代码时常见的「死文件」陷阱：**grep 到注册名不等于它真的被注册**，还要看导入链。

#### 4.2.4 代码实践

1. **实践目标**：用文件计数感受「模型组件是主体、系统是编排」的代码分布。
2. **操作步骤**：
   ```bash
   ls threestudio/models/geometry threestudio/models/guidance
   wc -l threestudio/systems/dreamcraft3d.py threestudio/data/image.py
   ```
3. **需要观察的现象**：`guidance/` 下的文件数量明显多于其他孙包（十余个引导实现，多数继承自 threestudio 上游，DreamCraft3D 主要新增/修改了 BSD、stable-zero123 等）；`dreamcraft3d.py` 是一个数百行的大文件，承载了全部训练逻辑。
4. **预期结果**：对「改哪个环节去哪个目录」形成条件反射。
5. 运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：我想新增一种自定义的背景模型，应该把文件放在哪里？

> **答案**：`threestudio/models/background/`（或通过 `custom_import` 放在包外，见 u3-l1）。文件内用 `@threestudio.register("my-background")` 注册后，配置里写 `background_type: my-background` 即可使用。

**练习 2**：`scripts/` 下的文件会被 `import threestudio` 自动导入吗？

> **答案**：不会。`threestudio/__init__.py` 只导入了 `data, models, systems`（见 4.3.3），`scripts` 不在链路上，它们是手动运行的独立入口（如 `python threestudio/scripts/img_to_mv.py ...`，见 [README.md:134](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L134)）。

### 4.3 注册机制精读：`threestudio/__init__.py` 的 register 与 find

#### 4.3.1 概念说明

整个框架的「插件化」只靠一个模块级字典实现。任何类只要被 `@threestudio.register("名字")` 装饰，就进入了全局注册表；任何代码只要调用 `threestudio.find("名字")`，就能拿到这个类。配置 yaml 里的 `*_type` 字符串正是 `find` 的键。这套机制的妙处在于：

- **配置驱动**：换一种几何表示只需改 yaml 里的一个字符串，不用改任何 Python 代码。
- **延迟解耦**：`systems/` 不需要 `import` 每一种 geometry/guidance，只通过注册表按名取用。
- **可扩展**：外部代码用 `custom_import` 注入自己的注册（u3-l1 实践）。

#### 4.3.2 核心流程

注册的写入与读取：

```text
写入：@threestudio.register("implicit-volume")
      └── 装饰器把类存入 __modules__["implicit-volume"] = ImplicitVolume 类

读取：threestudio.find(cfg.system.geometry_type)
      └── 返回 __modules__["implicit-volume"]，随后被调用实例化
```

注册发生的时机是「导入即注册」：

```text
import threestudio（launch.py 中）
   └── threestudio/__init__.py 末行: from . import data, models, systems
          ├── data/__init__.py   -> image, uncond          （注册 2 个 datamodule）
          ├── models/__init__.py -> 7 个孙包               （注册约 36 个模型组件）
          └── systems/__init__.py -> dreamcraft3d, zero123 （注册 2 个 system；
                dreamcraft3d 还导入 PerceptualLoss -> 再注册 perceptual-loss）
```

#### 4.3.3 源码精读

核心实现只有 13 行：

- [threestudio/__init__.py:L1-L13](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L1-L13)：第 1 行定义空字典 `__modules__`；`register(name)` 返回一个装饰器，把类 `cls` 存进 `__modules__[name]` 再原样返回；`find(name)` 直接按 key 取值（键不存在会抛 `KeyError`）。
- [threestudio/__init__.py:L36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L36)：`from . import data, models, systems`——全项目注册的总开关。中间第 16-33 行只是给日志加颜色/等级的语法糖，与注册无关。

注册的消费方在入口脚本里：

- [launch.py:L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L109)：`dm = threestudio.find(cfg.data_type)(cfg.data)`——用配置顶层的 `data_type` 找到数据模块类并实例化。
- [launch.py:L120-L122](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L120-L122)：`system = threestudio.find(cfg.system_type)(cfg.system, ...)`——同理找到系统类。系统内部再对 geometry/guidance 等子组件重复这一「find + 实例化」模式。

一个注册点的真实样子（数据模块用的是 `from threestudio import register` 后的短名装饰器）：

- [threestudio/data/image.py:L13-L19](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L13-L19)：`import threestudio` 与 `from threestudio import register`，同时从 `uncond` 复用随机相机组件——`image.py` 组合 `uncond.py` 的能力，正是「单图监督 + 随机相机」混合数据管线的伏笔（u4-l2 精讲）。
- [threestudio/data/image.py:L313](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/data/image.py#L313)：`@register("single-image-datamodule")`。

#### 4.3.4 代码实践

1. **实践目标**：在不启动训练的前提下，验证「导入即注册」并观察注册表内容。
2. **操作步骤**：在仓库根目录新建临时脚本（示例代码，不进入仓库）：
   ```python
   import threestudio
   names = sorted(threestudio.__modules__.keys())
   print(f"共注册 {len(names)} 个组件")
   for n in names:
       print(f"{n:45s} -> {threestudio.__modules__[n].__module__}")
   ```
   运行 `python 该脚本.py`。
3. **需要观察的现象**：打印的键包含 `dreamcraft3d-system`、`implicit-volume`、`stable-diffusion-bsd-guidance` 等；总数应为 **41**；每个键对应的 `__module__` 就是定义它的文件路径。注意 `single-image-datamodule` 只会指向 `threestudio.data.image`，而不是未被导入的 `images.py`。
4. **预期结果**：注册表内容 = yaml 里所有可能出现的 `*_type` 取值全集。若把脚本里的 `import threestudio` 换成 `import threestudio.utils`，绝大多数键会消失——因为没走到第 36 行的总导入。
5. 该脚本依赖 `pytorch_lightning` 等包可正常 import（无需 GPU）；运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果 yaml 里把 `geometry_type` 写成了一个不存在的名字（如 `implicit-volum` 少个 e），会发生什么？

> **答案**：`threestudio.find` 执行 `__modules__["implicit-volum"]` 时抛出 `KeyError`，训练在系统 configure 阶段直接崩溃。排错时先对照注册表确认拼写。

**练习 2**：为什么 `register` 装饰器最后要 `return cls`？

> **答案**：装饰器约定用返回值替换被装饰的名字。如果不返回 `cls`，模块里的 `ImplicitVolume` 会变成 `None`，后续任何直接引用该类名的代码（如类型注解、继承）都会失效。注册只是「副作用」，类本身必须原样交还。

**练习 3**：两个文件注册了同一个名字会怎样？

> **答案**：后导入者覆盖先导入者（字典赋值语义）。仓库里 `data/image.py` 与 `data/images.py` 都注册 `single-image-datamodule`，但 `data/__init__.py` 只导入前者，所以不冲突；若两者都被导入，则以导入顺序靠后的为准，且无任何告警——排查这类问题时务必检查导入链。

### 4.4 从 yaml 到代码：`*_type` 键与注册名对照

#### 4.4.1 概念说明

打开 [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml)，会看到成对出现的键：`geometry_type: "implicit-volume"` 与紧随其后的 `geometry: {...}`。规律是：**`X_type` 的值是注册名（决定用哪个类），`X` 是传给该类构造函数的参数**。这就是阅读任何 threestudio 系配置的万能钥匙。

#### 4.4.2 核心流程

```text
配置片段                              注册表查找                     实例化
--------------------------           ------------------------      --------------------------
system_type: dreamcraft3d-system -> find -> systems/dreamcraft3d.py 的类
  geometry_type: implicit-volume  -> find -> models/geometry/implicit_volume.py
  guidance_type: deep-floyd-guidance -> find -> models/guidance/deep_floyd_guidance.py
  guidance_3d_type: stable-zero123-guidance -> find -> .../stable_zero123_guidance.py
data_type: single-image-datamodule -> find -> data/image.py
```

#### 4.4.3 源码精读

以粗阶段配置为例，每个 `*_type` 都能在 4.3.4 打印出的注册表里找到唯一对应：

| 配置键 | 行号 | 取值 | 定义文件 |
| --- | --- | --- | --- |
| `data_type` | [configs/dreamcraft3d-coarse-nerf.yaml:L6](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L6) | `single-image-datamodule` | `threestudio/data/image.py` |
| `system_type` | [configs/dreamcraft3d-coarse-nerf.yaml:L41](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L41) | `dreamcraft3d-system` | `threestudio/systems/dreamcraft3d.py` |
| `geometry_type` | [configs/dreamcraft3d-coarse-nerf.yaml:L44](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L44) | `implicit-volume` | `threestudio/models/geometry/implicit_volume.py` |
| `material_type` | [configs/dreamcraft3d-coarse-nerf.yaml:L75](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L75) | `no-material` | `threestudio/models/materials/no_material.py` |
| `background_type` | [configs/dreamcraft3d-coarse-nerf.yaml:L79](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L79) | `solid-color-background` | `threestudio/models/background/solid_color_background.py` |
| `renderer_type` | [configs/dreamcraft3d-coarse-nerf.yaml:L81](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L81) | `nerf-volume-renderer` | `threestudio/models/renderers/nerf_volume_renderer.py` |
| `prompt_processor_type` | [configs/dreamcraft3d-coarse-nerf.yaml:L88](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L88) | `deep-floyd-prompt-processor` | `threestudio/models/prompt_processors/deepfloyd_prompt_processor.py` |
| `guidance_type` | [configs/dreamcraft3d-coarse-nerf.yaml:L94](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L94) | `deep-floyd-guidance` | `threestudio/models/guidance/deep_floyd_guidance.py` |
| `guidance_3d_type` | [configs/dreamcraft3d-coarse-nerf.yaml:L101](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L101) | `stable-zero123-guidance` | `threestudio/models/guidance/stable_zero123_guidance.py` |

补充说明：`system` 段下的 [L43](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L43) `stage: coarse` 不是注册名，而是 `dreamcraft3d-system` 自己的行为开关（同一系统在不同阶段走不同分支，u6-l1 展开）；`guidance_3d` 的权重路径指向 `./load/zero123/`（[L103-L104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L103-L104)），对应 4.1 中 `load/` 与 `extern/` 的分工。

#### 4.4.4 代码实践

1. **实践目标**：用纯文本工具完成「配置 → 源码文件」的定位，建立不依赖记忆的查表能力。
2. **操作步骤**：
   ```bash
   # 在仓库根目录执行；-r 递归，-n 显示行号
   grep -n '_type:' configs/dreamcraft3d-coarse-nerf.yaml
   grep -rn 'register("implicit-volume")' threestudio
   grep -rn 'register("dreamcraft3d-system")' threestudio
   ```
3. **需要观察的现象**：每条 `grep register` 都恰好命中一个文件（除 4.2.3 提到的 `single-image-datamodule` 重名例外）；四份配置的 `system_type` 全部相同（都是 `dreamcraft3d-system`），差异集中在 `geometry_type`/`renderer_type`/`guidance_type`。
4. **预期结果**：对任意 `*_type`，都能在 30 秒内跳到定义文件。
5. 运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：四份阶段配置共用同一个 `system_type`，那阶段差异体现在哪里？

> **答案**：体现在各组件的 `*_type` 与参数上：coarse 用 `implicit-volume` + `nerf-volume-renderer` + DeepFloyd/Zero123 双引导；texture 换成 DMTet + `nvdiff-rasterizer` + BSD 引导，并通过 `stage`、`fix_geometry` 等开关改变系统行为。详细对比在 u2-l3。

**练习 2**：`guidance_type` 和 `guidance_3d_type` 为什么是两个键？

> **答案**：粗阶段的系统同时挂两个扩散先验——文本条件先验（DeepFloyd，`guidance_type`）和视图条件先验（Zero123，`guidance_3d_type`），即 u1-l1 讲过的「双引导」。系统在 configure 时分别按这两个键 find 并实例化（u6-l1 精读源码）。

## 5. 综合实践

任务：**亲手绘制 DreamCraft3D 的代码地图，并用 grep 统计注册组件分布**（本讲核心实践，对应规格中的 practice_task）。

**步骤一：手绘目录树**。对照 4.1 与 4.2 的表格，画出仓库目录树（到 `threestudio/models/` 的孙包一级即可），在每个目录旁用一句话标注职责。

**步骤二：统计注册分布**。在仓库根目录执行：

```bash
# 模型/系统/感知损失用全名装饰器
grep -rn '@threestudio.register("' --include='*.py' threestudio | wc -l        # 预期 39
# 数据模块用短名装饰器
grep -rn '@register("' --include='*.py' threestudio/data | wc -l               # 预期 3
# 按目录聚合（看分布）
grep -rln '@threestudio.register("' --include='*.py' threestudio \
  | xargs -n1 dirname | sort | uniq -c | sort -rn
```

**步骤三：整理对照表**。把输出整理成「目录 → 数量 → 注册名列表」，并与下表核对（本讲编写时在 HEAD `5829ef1` 实测的分布）：

| 目录 | 生效注册数 | 注册名 |
| --- | --- | --- |
| `threestudio/models/guidance/` | 11 | stable-diffusion-guidance、stable-diffusion-vsd-guidance、stable-diffusion-bsd-guidance、stable-diffusion-unified-guidance、stable-diffusion-controlnet-guidance、stable-diffusion-controlnet-reg-guidance、deep-floyd-guidance、zero123-guidance、zero123-unified-guidance、stable-zero123-guidance、clip-guidance |
| `threestudio/models/materials/` | 6 | no-material、diffuse-with-point-light-material、neural-radiance-material、pbr-material、hybrid-rgb-latent-material、sd-latent-adapter-material |
| `threestudio/models/geometry/` | 5 | implicit-volume、implicit-sdf、volume-grid、tetrahedra-sdf-grid、custom-mesh |
| `threestudio/models/renderers/` | 5 | nerf-volume-renderer、neus-volume-renderer、nvdiff-rasterizer、gan-volume-renderer、patch-renderer |
| `threestudio/models/prompt_processors/` | 4 | deep-floyd-prompt-processor、stable-diffusion-prompt-processor、clip-prompt-processor、dummy-prompt-processor |
| `threestudio/models/background/` | 3 | solid-color-background、neural-environment-map-background、textured-background |
| `threestudio/systems/` | 2 | dreamcraft3d-system、zero123-system |
| `threestudio/models/exporters/` | 2 | mesh-exporter、dummy-exporter |
| `threestudio/data/` | 2（另有 1 个未导入的重复定义） | single-image-datamodule、random-camera-datamodule（images.py 中重复的 single-image-datamodule 不生效） |
| `threestudio/utils/perceptual/` | 1 | perceptual-loss |
| **合计** | **41** | |

**步骤四（进阶，可选）**：运行 4.3.4 的注册表打印脚本，确认 `len(threestudio.__modules__) == 41`，且与 grep 统计一致——这验证了「grep 到的装饰器」与「运行时真正注册的组件」一一对应（重名的 `images.py` 除外）。

**观察点与预期结果**：`guidance/` 是注册组件最多的目录（11 个），直观印证了 DreamCraft3D 的创新重心在扩散引导（BSD、视图条件 Zero123）；而四份配置实际用到的只是表中少数几个名字，其余是继承自 threestudio 上游的「备用件」。若你的统计数与 41 不符，优先检查：是否漏了 `@register(` 短名形式、是否把未导入的 `images.py` 也计入。

## 6. 本讲小结

- 仓库根目录由「三入口」（`launch.py`、`gradio_app.py`、`preprocess_image.py`）+「四目录」（`configs`、`threestudio`、`extern`、`load`）构成：配置在 `configs`、逻辑在 `threestudio`、外部代码在 `extern`、权重与资源在 `load`。
- `threestudio` 主包分 `systems / models / data / utils / scripts` 五大子包；`models` 再分 7 个孙包，对应三维流水线的 7 个可替换环节。
- 注册机制只有 13 行：[threestudio/__init__.py:L1-L13](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L1-L13) 的 `__modules__` 字典 + `register` 写入 + `find` 读取；注册的触发时机是包导入（`__init__.py` 第 36 行的总导入）。
- yaml 中 `X_type` 的值就是注册名，`X` 段是构造参数——「`X_type` 决定类，`X` 决定参数」是读所有配置的万能钥匙。
- 全项目共 41 个生效注册组件，其中 `guidance/` 占 11 个；grep 到装饰器还要核对导入链（`data/images.py` 的重名注册就不生效）。

## 7. 下一步学习建议

- 下一讲（u1-l4）将深入 `launch.py` 的 `main()`：看它如何加载配置、用 `find` 组装 datamodule 与 system、并挂载 callbacks 与 PyTorch Lightning Trainer——本讲的注册表将在那里被真正「消费」。
- 想提前理解配置的合并与覆盖，可先浏览 `threestudio/utils/config.py` 中的 `load_config`（u2-l2 精讲）。
- 对「自定义一个注册组件」感兴趣的读者，可以在学完 u3-l1 的注册机制扩展篇后，回到本讲的对照表，挑一个最简单的组件（如 `solid-color-background`）作为模仿模板。
