# u3-l3：BaseSystem 与 BaseLift3DSystem：三维系统的组装

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `BaseSystem` 如何把「注册表里的插件组件」接到「PyTorch Lightning 训练循环」上——它是配置系统（u2-l2）、注册机制（u3-l1）与 `BaseModule` 生命周期（u3-l2）三者的汇合点。
2. 读懂 `BaseLift3DSystem.configure()` 的组装逻辑：geometry / material / background / renderer 四类组件如何被 `find` 出来、互相注入，拼成一个可训练的三维系统。
3. 掌握 `geometry_convert_from` 的跨阶段衔接逻辑：DreamCraft3D 四阶段流水线中，检查点如何在「换几何表示」时把上一阶段的几何转换成下一阶段的初始值，以及它与 `system.weights`、`--resume` 的互斥关系。
4. 理解 `parse_optimizer` 如何用「点号模块路径」把 yaml 里的 `optimizer.params` 映射到不同的参数组、给不同模块设置不同学习率，并明白「没被列出的模块根本不会被优化」这一关键事实。

## 2. 前置知识

本讲建立在 u3-l1、u3-l2 之上，先快速回顾，再补充两个新概念。

**已建立的认识（来自前几讲）：**

- **注册机制**：yaml 中 `X_type` 的值是注册名（决定用哪个类），兄弟段 `X` 是构造参数；`threestudio.find(X_type)(cfg.X)` 即完成实例化（u3-l1）。
- **BaseModule 生命周期**：`parse_structured` 严格解析配置 → 绑定设备 → `configure()` 组装网络 → 可选加载权重，装权后用检查点里的 `epoch/global_step` 调 `do_update_step(on_load_weights=True)` 恢复渐进状态（u3-l2）。
- **update_step 钩子**：`Updateable` 组件靠 `update_step` / `update_step_end` 在每个批次前后刷新自身状态（分辨率爬坡、渐进视角等都靠它）（u3-l2）。

**本讲的新概念：**

- **PyTorch Lightning 的分工**：Lightning 是一个训练循环框架。`Trainer` 负责外围机械动作——循环 epoch/batch、调优化器、分布式、 checkpoint、日志；`pl.LightningModule`（用户继承）负责每一步的内容——`training_step` 算损失、`configure_optimizers` 声明优化器、各种 `on_train_batch_start` 钩子在特定时机被回调。你可以把 `Trainer` 想成「锅炉工」，`LightningModule` 想成「制定工艺的工程师」。
- **参数组（param group）**：PyTorch 优化器允许一次传入多组参数，每组带自己的超参（如不同的 `lr`）：`torch.optim.AdamW([{params: A, lr: 0.01}, {params: B, lr: 1e-5}])`。这是「几何编码学习率大、扩散模型学习率小」这类需求的标准做法。注意：**只有被列入参数组的张量会被优化**，没列的参数即使 `requires_grad=True` 也不会收到更新。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | 本讲主角。`BaseSystem`（LightningModule 封装）与 `BaseLift3DSystem`（四件套组装 + `geometry_convert_from` 衔接） |
| [threestudio/systems/utils.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py) | `parse_optimizer` / `parse_scheduler`，把 yaml 的 optimizer 段变成真实的优化器与调度器 |
| [configs/dreamcraft3d-texture.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml) | texture 阶段配置，含 `geometry_convert_from` 与四组 `optimizer.params`，是本讲的解剖标本 |
| [threestudio/systems/dreamcraft3d.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py) | `dreamcraft3d-system`，继承 `BaseLift3DSystem` 并在 `configure` 里追加引导模型（下一层组装的范例） |
| [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) | `load_module_weights`（按模块名抽取权重）、`find_last_path`（`@LAST` 占位符解析） |
| [launch.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py) | 消费现场：`find(cfg.system_type)(cfg.system, resumed=...)` 构建系统 |
| [threestudio/models/geometry/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py) 与 [threestudio/models/geometry/tetrahedra_sdf_grid.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py) | `create_from` 的默认实现与 DMTet 的真实转换逻辑 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① `BaseSystem` 桥接 Lightning；② `BaseLift3DSystem` 组装四件套；③ `geometry_convert_from` 跨阶段衔接；④ `parse_optimizer` 参数组映射。

### 4.1 BaseSystem：把注册组件接到 Lightning 训练循环上

#### 4.1.1 概念说明

u3-l1 讲过，`launch.py` 用 `threestudio.find(cfg.system_type)` 拿到系统类并实例化。但 `Trainer.fit(system, ...` 需要的不是一个普通对象，而是一个 `pl.LightningModule`——它得会算损失、会声明优化器、会响应各种批次钩子。`BaseSystem` 就是这层适配器：它继承 `pl.LightningModule` 拿到训练循环的全部接口，同时混入 `Updateable`（让自己也能参与 update_step 递归分发）和 `SaverMixin`（提供 `save_image_grid` 等落盘工具）。

一句话：**`BaseSystem` 是「注册组件世界」与「Lightning 训练循环世界」之间的翻译官。**

#### 4.1.2 核心流程

`BaseSystem.__init__` 的固定四步（与 u3-l2 的 BaseModule 生命周期同构，只是多出 loggers）：

```text
__init__(cfg, resumed)
 ├─ ① parse_structured(self.Config, cfg)   # yaml dict → 强类型 self.cfg
 ├─ ② create_loggers(cfg.loggers)           # wandb 等可选日志器
 ├─ ③ self.configure()                      # 抽象方法：子类在这里组装网络（虚函数，Base 自身为空）
 ├─ ④ 若 cfg.weights 非空 → load_weights()  # 整体热启动（strict=False）
 └─ ⑤ post_configure()                       # 装权后的钩子（Base 自身为空）
```

训练时的钩子分发（每批两次）：

```text
on_train_batch_start:  preprocess_data → 刷新 dataset → do_update_step(整棵组件树)
      ↓ Lightning 调 training_step（子类实现，本框架中是 dreamcraft3d-system）
on_train_batch_end:    刷新 dataset(end) → do_update_step_end(整棵组件树)
```

#### 4.1.3 源码精读

类声明与三重继承，注意它同时是 `Updateable`：

[threestudio/systems/base.py:21-30](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L21-L30) —— `BaseSystem(pl.LightningModule, Updateable, SaverMixin)`；内嵌 `Config` dataclass 只声明系统级的通用字段：`loggers/loss/optimizer/scheduler` 四个 dict、`weights` 与 `weights_ignore_modules`（热启动）、两个 `cleanup_after_*`（评估时释放显存）。这些字段对所有阶段配置通用，而 geometry/renderer 等专属字段留给子类 `BaseLift3DSystem.Config` 扩展。

[threestudio/systems/base.py:35-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L35-L48) —— 构造流程即上面伪代码的 ①-⑤。注意 `configure()` 在 `load_weights` **之前**调用：必须先有网络结构，才谈得上往里灌权重。`resumed` 标志被存下来（`self._resumed`），后面 `geometry_convert_from` 的分支要用它。

[launch.py:120-122](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L120-L122) —— 消费现场：`system = threestudio.find(cfg.system_type)(cfg.system, resumed=cfg.resume is not None)`。即只有命令行显式传了 `--resume` 时 `resumed=True`；这是 u1-l4 讲过的「断点自动续训」与这里的手动 `resumed` 标志的分工。

[threestudio/systems/base.py:50-56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L50-L56) —— `load_weights`：`load_module_weights` 读检查点拿到 `(state_dict, epoch, global_step)`，`load_state_dict(strict=False)` 宽松加载（键不完全对齐也不报错），随后调用 `do_update_step(epoch, global_step, on_load_weights=True)`。最后这行是 u3-l2 讲过的模式：恢复那些「依赖步数的状态」，例如哈希编码解锁到了第几层——否则从 5000 步检查点热启动的模型会以为自己在第 0 步。

[threestudio/systems/base.py:69-74](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L69-L74) —— `true_global_step` 属性：训练时就是 Lightning 的 `global_step`；但 `--export`/`--test` 这类不走 fit 的模式下，Lightning 的 `global_step` 停在 0，于是 u1-l4 讲过的 `set_resume_status`（[threestudio/systems/base.py:58-62](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L58-L62)）会把检查点里的真实步数写进 `_resumed_eval_status`，让本属性返回正确值。**全项目的损失权重调度、时间步区间都取自 `true_global_step` 而非裸 `global_step`，原因在此。**

[threestudio/systems/base.py:92-93](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L92-L93) —— 系统级便捷方法 `C(value)`：把 u3-l2 讲过的 C() 插值函数与当前 `epoch/global_step` 绑定，子系统代码里 `self.C(self.cfg.loss.lambda_sparsity)` 一行即得「此刻的权重」，例如从 `[2000, 5., 1., 2001]` 线性过渡。

[threestudio/systems/base.py:95-106](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L95-L106) —— `configure_optimizers`（Lightning 约定接口）：把 `self.cfg.optimizer` 交给本讲 4.4 的 `parse_optimizer`，可选再挂 `parse_scheduler`。**注意第二个参数是 `self`——系统本身就是被优化的模型，所以 yaml 里的模块路径都是从 system 根出发的。**

[threestudio/systems/base.py:174-178](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L174-L178) 与 [threestudio/systems/base.py:114-119](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L114-L119) —— `on_train_batch_start` / `on_train_batch_end`：先把 dataloader 的 dataset 刷上（数据侧的分辨率爬坡），再 `do_update_step` / `do_update_step_end` 递归刷新整棵组件树（u3-l2 讲过的后序遍历）。validation/test/predict 四套模式有完全对称的实现（[L180-L196](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L180-L196)）。这就回答了 u3-l2 留下的问题：**钩子的触发时机由谁决定？——由 `BaseSystem` 的这些 Lightning 回调决定，且都发生在 `training_step` 之前/之后。**

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `resumed` 标志与 `true_global_step` 的传播链，理解「为什么断点续训不会重复做几何转换」。

**操作步骤**（源码阅读型，不需要 GPU）：

1. 打开 [launch.py:120-122](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L120-L122)，记录 `resumed` 的取值来源。
2. 顺着调用进入 [threestudio/systems/base.py:35-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L35-L48)，找到 `self._resumed` 的赋值行，再找到 `resumed` property（[L64-L67](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L64-L67)）和 4.3 节将要讲的 `not self.resumed` 条件（[L249](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L249)）。
3. 画出这条链：`--resume 参数 → cfg.resume → resumed=... → self._resumed → configure() 的分支判断`。

**需要观察的现象**：`--resume` 与 `system.weights` 是两条独立的权重来源——前者走 Lightning 的 `trainer.fit(ckpt_path=...)` 恢复全部状态（包括优化器），后者只在本构造函数里 `load_state_dict` 一次。

**预期结果**：能口头回答「同时给 `--resume` 和 `system.geometry_convert_from` 会发生什么」（答案见 4.3 的三重条件）。

### 4.2 BaseLift3DSystem：geometry / material / background / renderer 四件套组装

#### 4.2.1 概念说明

`BaseSystem` 只提供了骨架，`configure()` 是空的。`BaseLift3DSystem`（"Lift" 指从 2D 提升到 3D）补上了三维系统的标准拼法：一个可训练的三维场景由四个可替换插件构成——

- **geometry**：三维表示本体（密度场 / SDF / DMTet 网格），决定「形状」；
- **material**：材质网络，把几何输出的特征变成颜色；
- **background**：背景（纯色 / 纹理），决定没被几何挡住的像素长什么样；
- **renderer**：可微渲染器，把前三者 + 相机参数变成一张图，是连接 3D 与 2D 损失的桥梁。

四者都通过注册机制 `find(*_type)` 实例化，这正是 u2-l3 看到的「四份配置只换 `*_type` 键就切换阶段」的代码落点。

#### 4.2.2 核心流程

`BaseLift3DSystem.configure()` 主干（省略 convert 分支，4.3 详解）：

```text
configure()
 ├─ geometry = find(geometry_type)(geometry_cfg)        # 有两条路径，见 4.3
 ├─ material  = find(material_type)(material_cfg)
 ├─ background = find(background_type)(background_cfg)
 └─ renderer  = find(renderer_type)(renderer_cfg,
                                  geometry=geometry,      # 依赖注入：
                                  material=material,      # 渲染器持有前三者的引用
                                  background=background)
```

注意组装顺序：renderer 最后建，因为它要接收前三个对象作为构造参数——这不是配置传递，而是**对象引用注入**，渲染器从此直接调用 `self.geometry(...)` 拿密度、`self.material(...)` 上色。

#### 4.2.3 源码精读

[threestudio/systems/base.py:211-239](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L211-L239) —— `BaseLift3DSystem.Config` 继承 `BaseSystem.Config`，为每类组件声明一对字段：`geometry_type: str` + `geometry: dict`（material/background/renderer/guidance/prompt_processor 同构），外加三个跨阶段字段（`geometry_convert_from` / `geometry_convert_inherit_texture` / `geometry_convert_override`，4.3 详解）和一个导出器字段 `exporter_type`（默认 `"mesh-exporter"`，训练时不参与，`--export` 时才用，见 u2-l4）。**字段命名与 yaml 一一对应，这是读任何 threestudio 系配置的通用语法。**

[threestudio/systems/base.py:285-297](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L285-L297) —— else 分支（无几何转换时的常规路径）：四行 `find(...)(...)` 完成四件套组装，renderer 的构造参数里显式传入 `geometry=... material=... background=...`。

[threestudio/systems/dreamcraft3d.py:40-63](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L40-L63) —— 真正被四份配置使用的 `dreamcraft3d-system`（类名 `ImageConditionDreamFusion`）如何扩展这套组装：第一行 `super().configure()` 先建好四件套，再追加 `guidance`、可选的 `guidance_3d`（`guidance_3d_type` 为空串则置 `None`——texture 阶段就是这样关闭 3D 先验的）、`prompt_processor`、`perceptual_loss` 与可选的 `control_guidance`。**这展示了框架的组装范式：子系统的 configure 永远先调 super 再加私货，guidance 族组件只挂在系统层，不进 BaseLift3DSystem。**

[threestudio/systems/base.py:311-317](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L311-L317) —— `on_predict_start`（即 `--export` 路径）：按 `exporter_type` 建 exporter，同样注入 system 已有的 geometry/material/background。**导出器与渲染器共享同一套三维组件，这保证了「导出的网格」与「训练时看到的画面」是同一个场景。**

#### 4.2.4 代码实践

**实践目标**：用最短路径验证「换 `*_type` 就换实现」的组装逻辑。

**操作步骤**（源码阅读型）：

1. 对照 [configs/dreamcraft3d-texture.yaml:46-69](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L46-L69)，抄下四个 `*_type` 的值：`tetrahedra-sdf-grid` / `no-material` / `solid-color-background` / `nvdiff-rasterizer`。
2. 用 u3-l1 实践产出的「注册名 → 文件」对照表，查出这四个名字分别注册在哪个文件。
3. 再对 `configs/dreamcraft3d-coarse-nerf.yaml` 重复一遍，观察哪几个槽位换了实现、哪几个没变。

**需要观察的现象**：四阶段之间 material（`no-material`）与 renderer 的更替规律——前三阶段体渲染、末阶段光栅化，而 `solid-color-background` 全程不变。

**预期结果**：得到一张「槽位 → 各阶段实现」的四列表格，与 u2-l3 的配置对比表互相印证（那張表从 yaml 视角看，这张从注册类视角看）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BaseLift3DSystem` 不把 guidance 也纳入四件套，而由 `dreamcraft3d-system` 自己挂？

**答案**：四件套是「参与可微渲染闭环」的场景组件（renderer 需要同时持有它们并协同前向），而 guidance 是 2D 扩散先验，只消费渲染结果、不参与渲染本身；且不同系统的 guidance 组合差异很大（DreamCraft3D 有 guidance / guidance_3d / control_guidance 三种，可各自为空），放进基类会让不需要引导的系统背上冗余配置。框架把它留给具体系统按需组装。

**练习 2**：`on_predict_start` 里构建 exporter 时为什么不重新构建 geometry，而是复用 system 的？

**答案**：`--export` 时 system 是从检查点恢复出来的（u2-l4：必须传 `resume=`），其 geometry 已载入训练好的权重；exporter 复用它才能导出训练成果。若重建，得到的是随机初始化的几何。

**练习 3**：若把某配置的 `renderer_type` 拼错成 `"nvdiff-rasterize"`，错误在哪个环节、以什么形式暴露？

**答案**：在 `configure()` 执行到 `threestudio.find("nvdiff-rasterize")` 时抛 `KeyError`（u3-l1 讲过 find 是裸字典查找，无友好报错）。暴露时机是系统构造瞬间，也就是 `launch.py` 的 `find(cfg.system_type)(...)` 那一行内部，训练尚未开始。

### 4.3 geometry_convert_from：跨阶段几何衔接

#### 4.3.1 概念说明

DreamCraft3D 四阶段使用不同的几何表示（implicit-volume → implicit-sdf → DMTet → DMTet），检查点接力有两种语义（u2-l3 从配置视角总结过，这里看代码实现）：

- **`system.weights`（整体热启动）**：表示**不变**或可整体对齐时（coarse-nerf → coarse-neus），直接 `load_state_dict` 恢复整个系统。
- **`system.geometry_convert_from`（几何转换）**：表示**变化**时（coarse-neus → geometry → texture），把上一阶段几何**实例化出来、抽取其几何信息、转换**成新表示的初始值。纹理能否沿用由 `geometry_convert_inherit_texture` 控制。

为什么需要转换而不能直接灌权重？因为 `implicit-sdf` 的参数是 SDF 网络的权重，而 `tetrahedra-sdf-grid` 的参数是离散 SDF 网格 + 纹理编码——两者参数空间完全不同，必须通过「在旧表示上采样、填入新表示」的方式迁移。

#### 4.3.2 核心流程

[threestudio/systems/base.py:246-284](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L246-L284) 的转换分支，八步：

```text
前提（三重条件同时成立）：
  geometry_convert_from 非空  且  weights 为空  且  not resumed

① 读上一阶段 trial 的 configs/parsed.yaml → prev_system_cfg
② prev_geometry_cfg = prev_system_cfg.geometry，再用 geometry_convert_override 覆盖若干键
③ prev_geometry = find(prev_system_cfg.geometry_type)(prev_geometry_cfg)   # 用旧配置建旧几何
④ load_module_weights(ckpt, module_name="geometry") → 只取 "geometry." 前缀的权重
⑤ prev_geometry.load_state_dict(...) + do_update_step(on_load_weights=True)  # 恢复渐进状态
⑥ prev_geometry.to(device)
⑦ self.geometry = find(self.cfg.geometry_type).create_from(
       prev_geometry, self.cfg.geometry, copy_net=geometry_convert_inherit_texture)
⑧ del prev_geometry; cleanup()   # 释放旧几何
```

#### 4.3.3 源码精读

[threestudio/systems/base.py:244-250](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L244-L250) —— 先用 `find_last_path` 把两个路径里的 `@LAST` 占位符解析成「最新的 trial 目录」（README 命令里 `outputs/dreamcraft3d-coarse-neus/$prompt@LAST/ckpts/last.ckpt` 的 `@LAST` 即由此处理，实现见 [threestudio/utils/misc.py:138-156](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L138-L156)：按目录名前缀过滤、倒序排序取第一个——trial 目录名带时间戳，字典序即时间序）。随后的三重条件含义：**给了 `weights` 就优先整体热启动，不做转换；`--resume` 续训时全部状态由检查点恢复，更不能转换**（否则会把已训练的几何覆盖回初始转换值）。三者互斥，`geometry_convert_from` 的优先级最低。

[threestudio/systems/base.py:251-267](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L251-L267) —— 关键巧思：**要用旧检查点，得先知道旧几何的构造参数，而这些参数记录在上一阶段 trial 目录落盘的 `parsed.yaml` 快照里**（u2-l4 讲过 ConfigSnapshotCallback）。代码从 ckpt 路径反推 `../configs/parsed.yaml`（相对 ckpt 所在的 `ckpts/` 目录，源码里标注了 `TODO: hard-coded relative path`，[L259](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L259)），load_config 读回后 `parse_structured(self.Config, prev_cfg.system)` 还原成 `BaseLift3DSystem.Config`，于是拿到 `geometry_type`（旧表示的注册名）与 `geometry`（旧参数）。[L264](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L264) 的 `geometry_convert_override` 允许对旧配置打补丁（注释举例 `isosurface_threshold`），DreamCraft3D 四份配置均未用到，保持默认空 dict。

[threestudio/systems/base.py:268-275](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L268-L275) —— `load_module_weights(..., module_name="geometry")`：该函数（[threestudio/utils/misc.py:54-60](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L54-L60)）用正则 `^geometry\.(.*)$` 从整个系统 state_dict 里只剥出 `geometry.` 前缀的键并去掉前缀——上一阶段的 renderer/guidance 权重一概不要。随后的 `do_update_step(on_load_weights=True)` 又是 u3-l2 的模式：把旧几何的渐进状态（如编码层级）恢复到保存时的步数，保证下一步采样 SDF 时行为与训练结束时一致。

[threestudio/models/geometry/base.py:42-48](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/base.py#L42-L48) —— `create_from` 是 `BaseGeometry` 上的静态方法，默认实现直接 `raise TypeError`：**「如何从旧几何建新几何」是每种几何表示自己定义的转换协议**，框架只约定签名 `(other, cfg, **kwargs) -> BaseGeometry`。

[threestudio/models/geometry/tetrahedra_sdf_grid.py:266-296](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L266-L296) —— DMTet 的 `create_from`（带 `@torch.no_grad()`，纯初始化不建梯度图）。当 `other` 也是 `TetrahedraSDFGrid` 时（geometry → texture 阶段正是这种情形，两者都用 `tetrahedra-sdf-grid`）：克隆 SDF 网格数据、包围盒与形变场；而 `copy_net=self.cfg.geometry_convert_inherit_texture`（[L281](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L281) 传入）决定是否连纹理网络（`encoding` + `feature_network`）的权重一并拷贝——texture 配置里 `geometry_convert_inherit_texture: true`（[configs/dreamcraft3d-texture.yaml:45](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L45)），即 geometry 阶段练出的纹理作为 texture 阶段起点，这就是「纹理继承」。跨表示转换则是另一条路：`other` 为 `ImplicitVolume`（[L297-L319](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L297-L319)）或 `ImplicitSDF`（[L320-L348](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L320-L348)，coarse-neus → geometry 阶段实际走这条）时，先对旧几何跑等值面提取得到网格，再把 `mesh.extras["grid_level"]` 采样进新 SDF 网格完成初始化；两者都不认识则 `raise TypeError`（[L349-L352](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L349-L352)）。

[threestudio/systems/base.py:283-286](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L283-L286) —— 转换完成后 `del prev_geometry; cleanup()` 立即释放旧几何（强制 GC，见 [threestudio/utils/misc.py:100-103](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L100-L103)）；else 分支则是普通的 `find(geometry_type)(geometry)` 从零构建——coarse-nerf 作为第一阶段走的就是这条路。

#### 4.3.4 代码实践

**实践目标**：把 README 的四阶段命令与 `configure()` 的两个分支对上号，画出检查点接力图。

**操作步骤**（源码阅读型）：

1. 阅读 [README.md:114-125](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/README.md#L114-L125) 的 Stage 1→3 命令，注意每条命令末尾传的是 `system.weights="$ckpt"` 还是 `system.geometry_convert_from="$ckpt"`，以及路径里的 `@LAST`。
2. 对每条命令标注它命中 [threestudio/systems/base.py:246-250](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L246-L250) 三重条件的哪个分支。
3. 画一张接力图：四个阶段为四个节点，边标注「接力方式（weights / convert_from + inherit_texture）」与「旧几何类型 → 新几何类型」。

**需要观察的现象**：coarse-neus 阶段用 `system.weights` 热启动时，`load_weights` 走 `strict=False`——implicit-volume 与 implicit-sdf 的参数名并不完全对齐，宽松加载意味着能对上的键生效、对不上的保持初始化。

**预期结果**：接力图为：coarse-nerf（从零）→[weights]→ coarse-neus →[convert_from]→ geometry（`implicit-sdf` → `tetrahedra-sdf-grid`，采样转换）→[convert_from + inherit_texture=true]→ texture（`tetrahedra-sdf-grid` → 同类，克隆 SDF + 拷贝纹理网络）。

#### 4.3.5 小练习与答案

**练习 1**：为什么转换分支要读 `parsed.yaml` 而不是把旧几何的构造参数也存进 ckpt？

**答案**：Lightning 的 ckpt 只存 state_dict 与少量训练状态（epoch/global_step），不存配置。框架利用 u2-l4 的配置快照机制（每个 trial 落盘 `configs/parsed.yaml`）作为配置的持久化侧车，读取路径虽是硬编码相对路径（源码 TODO 已标注），但依赖的正是「trial 目录结构固定」这一约定。这也解释了为什么**移动或改名 trial 目录会破坏 convert_from**。

**练习 2**：如果用户同一条命令既传了 `system.weights` 又传了 `system.geometry_convert_from`，几何会以哪种方式初始化？

**答案**：走 `system.weights` 整体热启动，`geometry_convert_from` 被三重条件短路（`not self.cfg.weights` 不成立），转换分支不执行。

**练习 3**：`load_module_weights(..., module_name="geometry")` 与 `load_weights`（无 module_name）取出的 state_dict 有何不同？

**答案**：前者用正则只抽取 `geometry.` 前缀的键并去掉前缀（给独立的旧几何对象用，键需无前缀才能对上）；后者返回系统级完整 state_dict（给 `self.load_state_dict` 用，键需保留 `geometry.` 等前缀）。一个是「零件图」，一个是「整机图」。

### 4.4 parse_optimizer / parse_scheduler：按模块名定制学习率

#### 4.4.1 概念说明

texture 阶段同时训练：几何的纹理编码（哈希网格）、纹理 MLP、BSD 的两个扩散 UNet——四者合理的量级差近千倍（0.01 vs 0.00001），必须分组设学习率。`parse_optimizer` 的设计是：**yaml 里 `optimizer.params` 的每个键是一个从 system 根出发的点号路径，值是该参数组的超参**。路径解析靠 `getattr_recursive` 逐段 `getattr`，与文件系统路径毫无关系，纯属性访问链。

还有一个容易踩坑的语义：**一旦配置了 `params`，只有被点名的模块会被优化**。没列出的模块（比如 material、background、renderer 的参数）不会进入任何参数组，也就不会收到任何更新——这是有意为之的阶段控制手段，而非遗漏。

#### 4.4.2 核心流程

```text
configure_optimizers()                     # base.py:95-106
 └─ parse_optimizer(cfg.optimizer, self)   # self 即 system，路径的起点
     ├─ 有 params 段？
     │    ├─ 是 → 对每个 (name, args)：
     │    │        module = getattr_recursive(system, name)   # "guidance.train_unet"
     │    │                  → system.guidance.train_unet
     │    │        参数组 = {params: module.parameters(), name: name, **args}   # args 里的 lr 等
     │    └─ 否 → 单组 model.parameters()
     ├─ 选优化器类：FusedAdam→apex；Adan→threestudio.systems.optimizers；其余→torch.optim
     └─ 返回 optim（可再挂 parse_scheduler 的调度器）
```

#### 4.4.3 源码精读

[threestudio/systems/utils.py:19-31](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py#L19-L31) —— 两个小工具：`getattr_recursive` 把 `"geometry.encoding"` 拆段循环 `getattr`，等价于 `system.geometry.encoding`；`get_parameters` 对取到的对象分三类——`nn.Module` 返回 `.parameters()` 迭代器，`nn.Parameter` 返回它本身（比如 DMTet 的 `sdf` 这种直接挂在模块上的裸参数），**其余返回空列表 `[]`（静默！）**——路径写错不会报错，只会让该组一个参数都没有。

[threestudio/systems/utils.py:34-53](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py#L34-L53) —— `parse_optimizer` 主体：`config.params.items()` 逐项构造参数组，`**args` 把 yaml 里该组的 `lr` 等键透传进组字典（PyTorch 参数组的标准写法）；`name` 也塞进组里只为调试可读。优化器类按三个来源解析：apex 的 FusedAdam、threestudio 自带的 Adan（见 `threestudio/systems/optimizers.py`）、其余从 `torch.optim` 按名字取（texture 配置的 `AdamW` 即 `torch.optim.AdamW`），`config.args`（`betas`、`eps`）作为全体组共享的构造参数。

[configs/dreamcraft3d-texture.yaml:139-152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L139-L152) —— texture 阶段的 optimizer 段，四个路径逐个对号入座：

| yaml 键 | 解析到的对象 | lr | 训练内容 |
| --- | --- | --- | --- |
| `geometry.encoding` | `system.geometry.encoding`（tinycudann 哈希网格，nn.Module） | 0.01 | 纹理的空间编码 |
| `geometry.feature_network` | `system.geometry.feature_network`（MLP，nn.Module） | 0.001 | 特征→颜色前的网络 |
| `guidance.train_unet` | `system.guidance.train_unet`（BSD 的可训练 UNet，nn.Module） | 0.00001 | DreamBooth 式个性化 |
| `guidance.train_unet_lora` | `system.guidance.train_unet_lora`（注入 LoRA 的 UNet） | 0.00001 | VSD/BSD 的 LoRA 侧 |

前两个是四件套（经 BaseLift3DSystem 组装，属性名 `geometry` 挂在 system 上）；后两个是 dreamcraft3d-system 在 `configure` 里追加的 guidance 的子属性（4.2.3）。**路径能打通的前提正是 4.2 的组装先于 `configure_optimizers` 发生**——后者要到 Lightning 开始 fit 才被调用。

[threestudio/models/geometry/tetrahedra_sdf_grid.py:80-92](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/geometry/tetrahedra_sdf_grid.py#L80-L92) —— 一个精妙的联动：texture 配置里 `geometry.fix_geometry: true`（[configs/dreamcraft3d-texture.yaml:59](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L59)）使 `sdf` 与 `deformation` **根本不注册为 nn.Parameter**（只有 `not fix_geometry` 时才走 `register_parameter`）。于是即便想训几何也没有参数可指——`fix_geometry` 从表示层、`optimizer.params` 从优化器层，双保险地冻结了几何，呼应 u2-l3 讲的「texture 阶段只练外观」。

[threestudio/systems/utils.py:74-104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py#L74-L104) —— `parse_scheduler`（DreamCraft3D 四份配置均未配置 `scheduler`，`BaseSystem.Config.scheduler` 默认 `None`，[threestudio/systems/base.py:100-105](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L100-L105) 的 if 分支不会进入）：把 `lr_scheduler` 名字映射到 `torch.optim.lr_scheduler` 的类，支持 `ChainedScheduler` / `SequentialLR` 递归组合，并要求 `interval` 为 `epoch` 或 `step`（Lightning 按此决定调度步进粒度）。顺带一提，学习率随步数的另一种更常用的做法是 u3-l2 讲的 C() 四元组插值直接写进 loss 权重，而非调度器。

#### 4.4.4 代码实践

**实践目标**：在不加载任何大模型的前提下，验证 `getattr_recursive` + `get_parameters` 的匹配规则，体会「路径写错会静默空组」。

**操作步骤**：在仓库外任意目录新建脚本 `match_params.py`（**示例代码**，非项目原有文件）：

```python
# 示例代码：用迷你模块树模拟 system 的属性结构，复现 parse_optimizer 的匹配逻辑
import torch.nn as nn
from threestudio.systems.utils import getattr_recursive, get_parameters

class FakeSystem(nn.Module):        # 模拟 dreamcraft3d-system 的属性树
    def __init__(self):
        super().__init__()
        self.geometry = nn.Module()          # 模拟 tetrahedra-sdf-grid
        self.geometry.encoding = nn.Linear(4, 4)
        self.geometry.feature_network = nn.Linear(4, 3)
        self.guidance = nn.Module()
        self.guidance.train_unet = nn.Linear(8, 8)
        self.guidance.train_unet_lora = nn.Linear(8, 8)

system = FakeSystem()
for name in ["geometry.encoding", "geometry.feature_network",
             "guidance.train_unet", "guidance.train_unet_lora",
             "geometry.typo_path"]:                     # 最后一个是故意的错误路径
    params = get_parameters(system, name)
    print(f"{name:32s} -> {sum(p.numel() for p in params)} 个参数")
```

**操作**：在装好 threestudio 依赖的环境里运行 `python match_params.py`（只需 torch 与本仓库在 `PYTHONPATH` 上）。

**需要观察的现象**：前四个路径各取到对应线性层的参数；`geometry.typo_path` 这一行**不报错**，打印 `0 个参数`。

**预期结果**：`getattr_recursive` 在错误路径上实际会抛 `AttributeError`（`getattr` 链断在 `typo_path`）；若把错误改成「存在的属性但非 Module/Parameter」（例如给 `geometry` 挂一个普通 int 属性再取它），才会走到 `return []` 的静默分支。两种失败模式都值得记下：前者响亮、后者无声。若在你的环境未运行，以上行为标注为**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：texture 配置没有列出 `background` 的参数组，solid-color-background 的颜色会随训练变化吗？

**答案**：不会。`parse_optimizer` 只要看到 `params` 段就只为列出的路径建组，background 的可学习参数不在任何组里，优化器不会更新它。（它仍可能通过 `update_step` 等机制被非梯度地修改，但那与优化器无关。）

**练习 2**：coarse 阶段配置的 `optimizer` 段没有 `params`（可自行查看 `configs/dreamcraft3d-coarse-nerf.yaml` 末尾），此时优化谁？

**答案**：走 [threestudio/systems/utils.py:41-42](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py#L41-L42) 的 else 分支 `model.parameters()`——system 的全部参数进一个组，统一使用 `args` 里声明的 `lr: 0.01`（见 [configs/dreamcraft3d-coarse-nerf.yaml:141-146](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L141-L146)）。被冻结（`requires_grad=False`）的组件虽在组内，但没有梯度、不会更新。

**练习 3**：为什么 texture 阶段 trainer 配置了 `strategy: "ddp_find_unused_parameters_true"`（[configs/dreamcraft3d-texture.yaml:161](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L161)）？

**答案**：BSD 的训练子步交替进行（u2-l3、后续 u7-l5 详解），每个子步只用到一个 UNet 子集，另一些可训练参数在该步的前向中未被使用、没有梯度；DDP 默认会因此报错。`find_unused_parameters=True` 让 DDP 容忍这种情况。这与本讲的联系是：**参数组声明了「谁可能被训」，而实际每步谁参与前向由 training_step 的调度决定，两者解耦。**

## 5. 综合实践

综合任务（本讲规格中的 practice_task）：**画出 `BaseLift3DSystem.configure` 的完整组装流程图（含 create_from 分支），并亲手完成 texture 配置 `optimizer.params` 的映射解释。**

**第一步：组装流程图。** 把 4.2 与 4.3 的两个分支合成一张图，形如：

```text
configure()
 │
 ├─ find_last_path(geometry_convert_from / weights)     # 解析 @LAST
 │
 ├─ [三重条件: convert_from 非空 ∧ weights 空 ∧ not resumed]
 │    ├─ 是 → 读上一 trial 的 configs/parsed.yaml
 │    │        重建旧几何 → 抽取 "geometry." 权重 → 恢复渐进状态
 │    │        → find(geometry_type).create_from(旧几何, cfg,
 │    │              copy_net=geometry_convert_inherit_texture)
 │    │        → del 旧几何 + cleanup()
 │    └─ 否 → find(geometry_type)(geometry)             # 从零构建
 │
 ├─ material  = find(material_type)(material)
 ├─ background = find(background_type)(background)
 └─ renderer  = find(renderer_type)(renderer, geometry=…, material=…, background=…)
      （dreamcraft3d-system 随后 super().configure() 之外再挂 guidance 等）
```

画好后自查三个细节是否包含：`create_from` 的 `copy_net` 来自哪个配置键；`parsed.yaml` 的相对路径如何从 ckpt 路径推出；三重条件里 `resumed` 的来源。

**第二步：optimizer 映射解释。** 对照 [configs/dreamcraft3d-texture.yaml:139-152](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-texture.yaml#L139-L152)，为四个路径各写一句话：解析链条（system 根 → 哪个组件的哪个属性 → 为什么该属性存在）、该组的学习率量级与其训练对象的匹配理由（纹理编码 0.01 / MLP 0.001 / 扩散 UNet 0.00001），并注明 `fix_geometry: true` 使 `geometry.sdf`、`geometry.deformation` 不再是 Parameter、与「params 未列出即不训」形成双保险。写成 Markdown 表格放进自己的笔记。

**完成标志**：拿着这张流程图和表格，不看讲义也能向别人解释「一条 `system.geometry_convert_from=...ckpt` 命令行从回车到几何初始化完成，中间发生了哪八件事」。

## 6. 本讲小结

- `BaseSystem` 是注册组件世界与 Lightning 训练循环世界之间的桥：构造按「解析配置 → 建 loggers → configure → 可选热启动 weights → post_configure」五步走；每批训练在 `training_step` 前后各做一次 update_step 递归分发。
- `true_global_step` 在非 fit 模式下从检查点恢复真实步数，是全项目步数感知调度的可信时间源；`self.C()` 把 C() 插值与它绑定。
- `BaseLift3DSystem.configure` 用注册机制组装 geometry/material/background/renderer 四件套，renderer 通过构造参数注入持有前三者的对象引用；`dreamcraft3d-system` 在 `super().configure()` 之后再挂 guidance 族组件。
- `geometry_convert_from` 实现跨表示衔接：读上一阶段 trial 的 `parsed.yaml` 重建旧几何 → 只抽 `geometry.` 前缀权重 → `create_from` 转换（`copy_net` 控制纹理继承）；与 `system.weights`、`--resume` 三者互斥，优先级最低。
- `parse_optimizer` 用 `getattr_recursive` 点号路径把 yaml 的 `optimizer.params` 映射到参数组；配置了 `params` 就只有被点名的模块被优化；路径错误可能响亮（AttributeError）也可能无声（空参数组）。
- `fix_geometry: true` 让 DMTet 的 sdf/deformation 不注册为参数，与 optimizer 白名单共同构成 texture 阶段「冻结几何、只训外观」的双保险。

## 7. 下一步学习建议

- 下一讲进入**单元四（数据与相机）**：`RandomCameraIterableDataset` 的相机采样与 `single-image-datamodule` 如何把参考图塞进 batch——那是 `BaseSystem.on_train_batch_start` 里「先刷新 dataset」一行的具体内容。
- 若想先看系统层全貌，可直接跳到 **u6-l1（dreamcraft3d-system 的 configure 与双引导组装）**，本讲 4.2.3 的 `super().configure()` 扩展在那里展开成完整数据流。
- `create_from` 的几何细节（等值面采样、SDF 网格初始化）在 **u5-l4（DMTet 与 nvdiffrast）** 精读，建议读完单元五后回看本讲 4.3 的第⑦步。
- 建议顺手通读 [threestudio/systems/utils.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/utils.py)（全文仅百余行）与 [threestudio/systems/optimizers.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/optimizers.py)，把优化器解析的旁支（Adan、调度器组合）补齐。
