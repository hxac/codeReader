# Web-UI 与 App 交互

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清 `swift web-ui` 与 `swift app` 两条入口的区别与各自定位：前者是「全功能控制台」（训练/推理/导出/评测/采样一屏搞定），后者是「对话试用台」（围绕一个已部署模型做聊天）。
- 理解 `swift/ui/` 模块如何用「元数据驱动 + Gradio 猴子补丁」让一张表单的字段（`elem_id`）与 `arguments` 数据类的字段自动对齐，从而**复用 arguments 而非重复声明**。
- 跟踪 `BaseUI.build_ui` → `do_build_ui` → `sub_ui` 的界面构建流程，并理解 `update_input_model` 如何在切换模型时自动回填表单。
- 看懂 UI 提交后如何把表单值翻译成一条 `swift sft/rlhf ...` 命令、以子进程方式驱动真实 pipeline，以及 `swift app` 如何用 `InferClient` 直连 `swift deploy` 起的服务。

## 2. 前置知识

本讲是扩展机制单元（u10）的一篇，依赖前面已建立的认知，下面只做最小衔接，不重复展开：

- **CLI 分发**（u1-l4）：`swift <子命令>` 经 `cli_main` 的 `ROUTE_MAPPING` 路由到对应脚本，是否套 `torchrun` 由 `use_torchrun()` 判定，仅 `pt/sft/rlhf/infer` 走多进程。本讲的 `web-ui`/`app` **不在**多进程集合里。
- **Arguments 数据类体系**（u2-l1）：`BaseArguments` 用多继承把 `DataArguments`/`ModelArguments`/`TemplateArguments` 等拼成统一参数对象。本讲的 UI 表单本质是这些 dataclass 字段的**可视化镜像**。
- **推理引擎抽象**（u6-l1）：`InferClient` 走 HTTP 调 OpenAI 兼容服务，与本地推理对上层透明。`swift app` 的对话界面正是建立在 `InferClient` 上。
- **部署与服务化**（u8-l2）：`swift deploy` 起一个 OpenAI 兼容的常驻 HTTP 服务。`swift app` 在未指定 `--base_url` 时会自动 `run_deploy` 拉起这样一个服务。

> 关键术语：`BaseUI`（UI 基类）、`elem_id`（表单字段标识，等于参数名）、`update_data`（Gradio 组件 `__init__` 的猴子补丁）、`choice_dict`/`default_dict`/`arguments`（三张元数据表）、`InferClient`（HTTP 推理客户端）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `swift/ui/app.py` | `swift web-ui` 的管道 `SwiftWebUI`，编排 7 个功能 Tab 并启动 Gradio。 |
| `swift/ui/base.py` | UI 基类 `BaseUI`：元数据表、`build_ui` 编排、`update_data` 猴子补丁、模型自动回填 `update_input_model`。 |
| `swift/ui/llm_train/llm_train.py` | 训练 Tab `LLMTrain`：表单布局 `do_build_ui` 与表单→命令转换 `train`。 |
| `swift/ui/llm_train/model.py` | 训练 Tab 的「模型」子面板 `Model`，演示 `elem_id` 如何挂接参数。 |
| `swift/pipelines/app/app.py` | `swift app` 的管道 `SwiftApp`：自动部署 + 拉起对话界面。 |
| `swift/pipelines/app/build_ui.py` | 对话界面构建函数 `build_ui`，用 `InferClient` 跑流式聊天。 |
| `swift/arguments/webui_args.py` | `WebUIArguments`：`web-ui` 的 4 个启动参数。 |
| `swift/arguments/app_args.py` | `AppArguments`：继承 `WebUIArguments + DeployArguments`，`app` 的参数。 |
| `swift/cli/main.py` | `ROUTE_MAPPING` 路由表与 `use_torchrun` 判定。 |

## 4. 核心概念与源码讲解

### 4.1 web-ui 启动与模块组织

#### 4.1.1 概念说明

ms-swift 提供两条「零门槛」可视化入口：

- **`swift web-ui`**：一个把训练、强化学习、推理、导出、评测、采样全部装进 Gradio Tabs 的**全功能控制台**。用户在网页上勾选参数、点「开始训练」，后台就跑起一条真实的 `swift sft` 命令。它面向「不想写命令行、但要控制全部参数」的场景。
- **`swift app`**：一个**对话试用台**，围绕一个模型做聊天（含多模态上传）。它面向「模型已就绪、想快速试效果」的场景，本身不暴露训练参数。

两者都基于 Gradio，但组织方式完全不同：`web-ui` 用一套自研的 `BaseUI` 元数据框架驱动几十个表单字段；`app` 只是一个手写的聊天界面函数 `build_ui`。理解这种「重 vs 轻」的分工是本讲的主线。

#### 4.1.2 核心流程

`swift web-ui` 的启动链路：

1. 命令行 `swift web-ui ...` → `cli_main` 查 `ROUTE_MAPPING['web-ui']` → 路由到 `swift.cli.web_ui`。
2. `web_ui.py` 调 `webui_main()` → `SwiftWebUI(args).main()`。
3. `SwiftWebUI.run()` 读语言/端口/分享等参数，依次 `set_lang`，在 `gr.Blocks` 内用 `gr.Tabs()` 摆出 7 个 Tab，每个 Tab 由对应 `LLM*` 类的 `build_ui` 构建。
4. 给每个 Tab 注册一个 `app.load` 钩子，页面打开时自动触发 `update_input_model`，把当前模型路径对应的参数回填到表单。
5. `app.queue(...).launch(...)` 常驻服务。

关键点：`web-ui` 不在 `use_torchrun` 的多进程集合里，所以它本身是单进程 Gradio 服务；真正的多卡训练发生在用户点「提交」后**另起的 `swift sft` 子进程**中（见 4.4）。

#### 4.1.3 源码精读

`ROUTE_MAPPING` 把 `web-ui` 与 `app` 都登记在册，二者均不在 `{'pt','sft','rlhf','infer'}` 多进程集合内：

- [swift/cli/main.py:14-27](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L14-L27)：路由表，`'web-ui': 'swift.cli.web_ui'`、`'app': 'swift.cli.app'`。
- [swift/cli/main.py:30-35](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L30-L35)：`use_torchrun` 仅看 `NPROC_PER_NODE`/`NNODES` 环境变量，与子命令名无关；后续判定才决定哪些子命令真正套 torchrun。

CLI 入口极薄，仅转发到 `webui_main`：

- [swift/cli/web_ui.py:1-6](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/web_ui.py#L1-L6)：`webui_main()` 是真正入口。

`SwiftWebUI` 继承 `SwiftPipeline`，`args_class = WebUIArguments`，业务在 `run()`：

- [swift/ui/app.py:44-49](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/app.py#L44-L49)：`SwiftWebUI` 声明，`run()` 入口。
- [swift/ui/app.py:63-77](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/app.py#L63-L77)：在 `gr.Blocks` 内 `gr.Tabs()` 摆出 7 个 Tab——`LLMTrain`/`LLMRLHF`/`LLMGRPO`/`LLMInfer`/`LLMExport`/`LLMEval`/`LLMSample`，每个调 `XXX.build_ui(XXX)`。
- [swift/ui/app.py:82-109](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/app.py#L82-L109)：为每个 Tab 注册 `app.load`，页面加载即触发 `update_input_model`，输入是 `model` 字段、输出是该 Tab 全部有效字段。
- [swift/ui/app.py:110](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/app.py#L110)：`app.queue(...).launch(server_name=server, inbrowser=True, server_port=port, height=800, share=share)` 常驻。

`WebUIArguments` 只有 4 个启动参数，分别控制绑定地址、端口、公网分享链接、界面语言：

- [swift/arguments/webui_args.py:5-18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/webui_args.py#L5-L18)：`server_name`/`server_port`/`share`/`lang`。

`run()` 还支持环境变量覆盖（`WEBUI_SHARE`/`WEBUI_SERVER`/`WEBUI_PORT`/`SWIFT_UI_LANG`），优先级是「环境变量 > 命令行参数」：

- [swift/ui/app.py:50-55](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/app.py#L50-L55)：环境变量读取逻辑。

#### 4.1.4 代码实践

1. **实践目标**：确认 `swift web-ui` 安装可用并观察其启动行为。
2. **操作步骤**：
   - 执行 `swift web-ui --server_port 7860 --lang zh`（若无 GPU 也可启动，UI 仅是前端）。
   - 浏览器打开 `http://localhost:7860`，依次点击 7 个 Tab：训练 / RLHF / GRPO / 推理 / 导出 / 评测 / 采样。
   - 在「训练」Tab 的模型框里填一个本地 checkpoint 路径（含 `args.json`），观察其它字段是否自动变化。
3. **需要观察的现象**：页面打开瞬间各 Tab 字段是否被自动回填；切换模型后 `template`/`model_type`/`system` 是否联动。
4. **预期结果**：7 个 Tab 均可渲染；填入带 `args.json` 的目录后字段自动回填（机制见 4.3）。
5. 若本地无 GPU/无模型，UI 仍可打开，但「开始训练」会因缺数据/缺卡而失败——属正常，**待本地验证**实际训练提交。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `swift web-ui` 本身是单进程，却能让用户发起多卡训练？

**参考答案**：`web-ui` 不在 `use_torchrun` 的多进程集合里，它只负责渲染表单；用户点「提交」后，`LLMTrain.train` 会拼出一条带 `CUDA_VISIBLE_DEVICES`/`NPROC_PER_NODE` 的 `swift sft` 命令并以子进程方式启动（见 4.4），多卡训练发生在那个子进程里。

**练习 2**：`WebUIArguments` 只有 4 个字段，但训练 Tab 上有几十个可调参数，这几十个参数从哪里来？

**参考答案**：它们不是 `WebUIArguments` 的字段，而是 `RLHFArguments`（训练/RLHF/GRPO 共用）等业务 dataclass 的字段；UI 通过 `BaseUI.get_choices_from_dataclass(RLHFArguments)` 等方法把它们「镜像」成表单（见 4.2）。

### 4.2 BaseUI 元数据驱动：表单如何复用 arguments

#### 4.2.1 概念说明

如果用「纯手写」方式做训练表单，每加一个参数就要同时改 dataclass、改 UI、改命令拼接三处，极易漂移。ms-swift 的做法是**让表单字段直接镜像 dataclass 字段**：UI 不重复声明参数语义，而是用一个 `elem_id`（= 参数名）去三张元数据表里查「候选值/默认值/界面文案」，由 dataclass 单一来源驱动 UI。

这套机制的核心是 `BaseUI` 基类与一个对 Gradio 组件 `__init__` 的**猴子补丁** `update_data`：拦截每一次 `gr.Textbox(elem_id=...)`/`gr.Dropdown(...)` 的构造，自动注入 choices/value/label/info。这样 `do_build_ui` 里写的就只是「布局 + elem_id」，参数语义全部来自 dataclass。

#### 4.2.2 核心流程

元数据从 dataclass 流向表单的流程：

1. **建表**：UI 子类在类体里调 `BaseUI.get_choices_from_dataclass(RLHFArguments)` → `choice_dict`；`get_default_value_from_dataclass` → `default_dict`；`get_argument_names` → `arguments`（`字段名 → '--字段名'`）。
2. **建表依据**：`get_choices_from_dataclass` 遍历 dataclass 字段，从 `Literal[...]` 类型或 `field(metadata={'choices': [...]})` 提取候选值；从字段默认值提取默认值。
3. **构建时**：`build_ui` 把全局 `builder` 指向当前 UI 类、`base_builder` 指向根 Tab 类；随后 `do_build_ui` 里每创建一个 Gradio 组件，被补丁的 `__init__` 就用 `elem_id` 去查这三张表，填入 choices/value/label/info。
4. **运行时**：组件值变化时，用 `cls.element('xxx')` 取回组件对象绑定回调；用 `cls.valid_elements()` 取全部「可提交」字段。

#### 4.2.3 源码精读

`update_data` 是核心补丁：它包裹 Gradio 组件的 `__init__`，按 `elem_id` 查 `choice`/`default`/`locale`/`argument` 并注入 kwargs：

- [swift/ui/base.py:33-78](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L33-L78)：`update_data` 装饰器。注意 L40-44 用 `base_builder.choice(elem_id)` 注入 choices，L52-53 用 `base_builder.default(elem_id)` 注入默认值，L55-68 用 `builder.locale(...)` 注入 label/info/value 并在 label 后追加上 `(--参数名)`，L74-75 把组件登记进 `builder.element_dict[elem_id]`。

模块导入时一次性替换全部常用 Gradio 组件的 `__init__`：

- [swift/ui/base.py:81-91](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L81-L91)：`Textbox.__init__ = update_data(Textbox.__init__)` 等十一行，覆盖 Textbox/Dropdown/Checkbox/Slider/TabItem/Accordion/Button/File/Image/Video/Audio。

三张元数据表的提取函数——直接读 dataclass 字段：

- [swift/ui/base.py:267-287](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L267-L287)：`get_choices_from_dataclass`，从 `Literal` 类型或 `metadata['choices']` 取候选；若默认值不在候选里则插到首位。
- [swift/ui/base.py:289-304](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L289-L304)：`get_default_value_from_dataclass`，取字段默认值（含 `default_factory`），list 默认值会被 ` `.join 成字符串。
- [swift/ui/base.py:306-311](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L306-L311)：`get_argument_names`，`字段名 → '--字段名'`。

`LLMTrain` 用一行就把 `RLHFArguments` 的全部字段镜像成表单元数据：

- [swift/ui/llm_train/llm_train.py:237-239](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L237-L239)：`choice_dict`/`default_dict`/`arguments` 三表均来自 `RLHFArguments`。这就是「UI 复用 arguments」的落点——参数加到 `RLHFArguments`，表单自动就有对应字段。

`valid_elements` 决定哪些组件参与「提交」（只收 Textbox/Dropdown/Slider/Checkbox，且排除 `train_record`）：

- [swift/ui/base.py:231-237](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L231-L237)：`valid_elements` 过滤逻辑。

#### 4.2.4 代码实践

1. **实践目标**：验证「改 dataclass 即改表单」的元数据驱动机制。
2. **操作步骤**：
   - 在 Python 里执行（不启动服务）：
     ```python
     from swift.ui.base import BaseUI
     from swift.arguments import RLHFArguments
     print(BaseUI.get_choices_from_dataclass(RLHFArguments).get('tuner_type'))
     print(BaseUI.get_default_value_from_dataclass(RLHFArguments).get('learning_rate'))
     print(BaseUI.get_argument_names(RLHFArguments).get('lora_rank'))
     ```
   - 对照训练 Tab 上对应字段的候选值、默认值与 label 末尾的 `(--tuner_type)` 标注。
3. **需要观察的现象**：打印出的候选值是否与下拉框一致；`learning_rate` 默认值是否与表单初值一致。
4. **预期结果**：三者完全对应，证明表单字段由 dataclass 单一来源驱动。
5. 若想进一步验证，可临时给 `RLHFArguments` 加一个带 `Literal` 类型的新字段并重启 `web-ui`，观察表单是否多出一项——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`update_data` 为什么要同时维护 `builder` 与 `base_builder` 两个全局变量？

**参考答案**：`builder` 是「当前正在构建的子 UI 类」，提供 `locale`（界面文案）与 `element_dict`（组件登记）；`base_builder` 是「根 Tab 类」，提供 `choice`/`default`/`argument`（来自 dataclass 的元数据）。两者分离使得「文案随子面板变、元数据随根 Tab 统一」。

**练习 2**：表单里某个 `Dropdown` 没有显式传 `choices`，它的候选值从哪来？

**参考答案**：由 `update_data` 在构造时用 `elem_id` 查 `base_builder.choice(elem_id)` 自动注入，而 `choice` 最终回溯到 `choice_dict`——后者由 `get_choices_from_dataclass` 从 dataclass 的 `Literal`/`metadata['choices']` 提取。

### 4.3 build_ui 界面构建与模型自动回填

#### 4.3.1 概念说明

`build_ui` 既要解决「界面怎么摆」，也要解决「树形子面板怎么递归构建」。ms-swift 用模板方法模式：`BaseUI.build_ui` 固定「设全局 builder → 调 `do_build_ui` → 恢复 → 对 sub_ui 调 `after_build_ui`」骨架，子类只覆写 `do_build_ui`（布局）与 `after_build_ui`（跨组件回调绑定）。

另一个关键体验是**模型自动回填**：用户在模型框填一个路径，UI 立刻判断它是「Hub 模型 id」还是「本地 checkpoint」，并据此回填 `template`/`model_type`/`system` 等字段——这正是 u1-l5 所述「训练即所见，推理即所得」在 UI 侧的体现。其底层与 u2-l1 的 `from_pretrained`/`load_args_from_ckpt` 三档回载同源。

#### 4.3.2 核心流程

界面构建流程：

1. `SwiftWebUI.run()` 调 `LLMTrain.build_ui(LLMTrain)`（根 Tab 类既是 builder 也是 base_builder）。
2. `build_ui` 保存旧全局、设 `builder=LLMTrain`/`base_builder=LLMTrain`、调 `LLMTrain.do_build_ui`。
3. `do_build_ui` 用 `gr.TabItem`/`gr.Accordion`/`gr.Row` 摆布局，并递归调各 `sub_ui`（Model/Dataset/Runtime/...）的 `build_ui`。
4. 构建完后，因 `cls is base_tab`，遍历 `sub_ui` 调 `after_build_ui` 绑定跨组件回调（如「模型变化 → 回填表单」）。

模型回填流程（`update_input_model`）：

1. 取当前 Tab 全部 `valid_element_keys`；若模型为空，返回全 `gr.update()`（不动）。
2. 用 `get_matched_model_meta(model)` 匹配模型元信息；同时看本地有无 `args.json`。
3. 若两者都没有 → 弹「无法识别」提示并返回。
4. 若有 `args.json`（本地 checkpoint）→ 用对应 `arg_cls`（如 `RLHFArguments`）以 `resume_from_checkpoint` 或 `adapters`/`model` 方式实例化，把字段值灌回表单（`dataset` 等列表字段做 join）。
5. 若只有模型元信息（Hub 模型）→ 仅回填 `template`/`model_type`/`system`（`system` 取 `TEMPLATE_MAPPING[meta.template].default_system`，GRPO Tab 取专用 `DEFAULT_GRPO_SYSTEM`）。

#### 4.3.3 源码精读

`build_ui` 模板方法骨架：

- [swift/ui/base.py:120-134](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L120-L134)：保存/恢复全局 `builder`/`base_builder`，调 `do_build_ui`，根 Tab 时遍历 `sub_ui` 调 `after_build_ui`。
- [swift/ui/base.py:136-143](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L136-L143)：`after_build_ui` 默认空实现，留给子类绑跨组件回调。

`LLMTrain.do_build_ui` 布局训练表单，逐个 `gr.Dropdown(elem_id='tuner_type', ...)` 等，并递归构建子面板：

- [swift/ui/llm_train/llm_train.py:39-52](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L39-L52)：`sub_ui` 列表，Model/Dataset/Runtime/Save/Optimizer/Task/Tuner/Hyper/Quantization/SelfCog/Advanced/ReportTo。
- [swift/ui/llm_train/llm_train.py:242-292](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L242-L292)：`do_build_ui` 主体，注意 L253-273 各组件只用 `elem_id` 声明，候选/默认由补丁注入。

`Model` 子面板声明 `model`/`model_type`/`template`/`system` 等核心字段，并在 `after_build_ui` 绑定「模型变化 → 回填」：

- [swift/ui/llm_train/model.py:92-115](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/model.py#L92-L115)：`Model.do_build_ui`，`model` 下拉默认 `Qwen/Qwen2.5-7B-Instruct`、`allow_custom_value=True`，`template` 候选为 `TEMPLATE_MAPPING.keys()`。
- [swift/ui/llm_train/model.py:117-127](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/model.py#L117-L127)：`after_build_ui` 绑定两条回调——`model.change → update_input_model`（回填字段）与 `train_record.change → update_all_settings`（从历史记录恢复整套参数）。

`update_input_model` 实现「模型路径 → 表单回填」，分本地 checkpoint 与 Hub 模型两支：

- [swift/ui/base.py:313-340](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L313-L340)：函数签名与空模型/无法识别两支短路。
- [swift/ui/base.py:342-377](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L342-L377)：本地 `args.json` 支——用 `arg_cls(resume_from_checkpoint=model, load_data_args=True)`（或 `adapters`/`model`）实例化，逐字段 `gr.update(value=...)` 回填，`train_record` 给出历史记录列表。
- [swift/ui/base.py:378-407](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L378-L407)：Hub 模型支——仅回填 `template`/`model_type`/`system`，`system` 取 `TEMPLATE_MAPPING[model_meta.template].default_system`，GRPO Tab 取 `DEFAULT_GRPO_SYSTEM`（[swift/ui/base.py:25-30](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L25-L30)）。

历史记录缓存机制（按模型路径存参数快照，时间戳为文件名）：

- [swift/ui/base.py:146-182](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/base.py#L146-L182)：`save_cache`/`list_cache`/`load_cache`/`clear_cache`，缓存目录为 `~/.cache/modelscope/swift-web-ui`。

#### 4.3.4 代码实践

1. **实践目标**：跟踪 `build_ui` 的递归构建与 `update_input_model` 的回填分支。
2. **操作步骤**：
   - 在 `swift/ui/base.py` 的 `update_input_model` 第 332 行（`model_meta = get_matched_model_meta(model)`）后 mentally 跟踪两种输入：
     - 输入 A：`Qwen/Qwen2.5-7B-Instruct`（Hub 模型，无本地 args.json）→ 走 L378-407 分支。
     - 输入 B：一个本地 SFT 输出目录（含 `args.json`）→ 走 L342-377 分支。
   - 启动 `swift web-ui`，在训练 Tab 分别填入 A 与 B，观察回填字段范围差异。
3. **需要观察的现象**：A 只回填 `template`/`model_type`/`system`；B 几乎回填全部字段（含 `dataset`/`output_dir` 等）并出现「训练记录」下拉。
4. **预期结果**：与上述两支逻辑一致。
5. 若无可用本地 checkpoint，B 分支可暂用「待本地验证」，但可读 `args.json` 字段对照 L360-372 的回填键确认逻辑。

#### 4.3.5 小练习与答案

**练习 1**：`build_ui` 为何在 `cls is base_tab` 时才遍历 `sub_ui` 调 `after_build_ui`？

**参考答案**：`sub_ui` 的 `build_ui` 在递归时会把 `base_builder` 传成根 Tab，但 `after_build_ui` 绑定的是**跨子面板**的回调（如 `model.change` 触发整 Tab 回填），必须在根 Tab 的全部子面板都构建完之后才能绑定，否则引用的组件对象还不存在。

**练习 2**：`update_input_model` 里对本地 checkpoint 优先尝试 `resume_from_checkpoint`，失败且报错含 `using --model` 时回退到 `model=...`，这是为什么？

**参考答案**：不同 `arg_cls` 对「从目录加载」的支持不同——有的支持 `resume_from_checkpoint`，有的只接受 `--model`。这是一处兼容多参数类的「脏修复」（代码注释也标注 `TODO a dirty fix`），用异常信息分支来兜底。

### 4.4 表单到 pipeline：train 组装命令 与 swift app 对话界面

#### 4.4.1 概念说明

UI 的最终职责是**驱动 pipeline**。ms-swift 的做法很务实：`web-ui` 不在进程内直接调 `SwiftSft`，而是把表单值翻译成一条 `swift sft/rlhf ...` 命令字符串 + 环境变量，以子进程方式启动。好处是：UI 进程与训练进程完全解耦，训练崩溃不会拖垮 UI，且命令字符串就是用户手写 CLI 时的等价物——「所见即所执」。

`swift app` 则走另一条路：它不组装训练命令，而是用 `run_deploy` 拉起一个 `swift deploy` 服务，再用 `InferClient` 连上去跑对话。这是 u8-l2 部署能力的「前端壳」。

#### 4.4.2 核心流程

`LLMTrain.train`（表单 → 命令）流程：

1. 收集 `valid_element_keys` 与全部组件值；用正则把字符串值还原为 int/float/bool。
2. 与 `RLHFArguments` 默认值比对，**只保留与默认值不同的字段**（避免命令冗长）。
3. 处理 `more_params`（JSON 或 `--xxx xxx` 自由文本）与 `train_stage`（pt/sft）。
4. 若 `model` 指向含 `args.json` 的 checkpoint，改写为「基座 + resume_from_checkpoint/adapter」。
5. 实例化 `RLHFArguments(**kwargs)` 仅为算出规范化的 `output_dir`/`logging_dir`。
6. 拼接 `swift <cmd> --k v ...` 命令列表与 `CUDA_VISIBLE_DEVICES`/`NPROC_PER_NODE`/自定义 envs。
7. `train_local` 用 `run_command_in_background_with_popen` 后台启动，并刷新「运行中任务」列表。

`swift app`（部署 → 对话）流程：

1. `SwiftApp.run()`：若 `args.base_url` 为空 → `run_deploy(args, return_url=True)` 拉起临时部署服务（上下文管理器，退出即杀）。
2. `build_ui(base_url, ...)` 构建聊天界面，内部 `InferClient(base_url=base_url)` 连服务。
3. 按 `infer_backend` 选并发度（transformers=1，加速后端=16），`demo.queue(...).launch(...)`。

#### 4.4.3 源码精读

`LLMTrain.train` 把表单值清洗为「与默认不同的 kwargs」：

- [swift/ui/llm_train/llm_train.py:324-383](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L324-L383)：值类型还原（int/float/bool 正则）、与默认值比对只留差异字段、`more_params` 解析、checkpoint 改写、缺数据集报错。

拼装 `swift` 命令与环境变量：

- [swift/ui/llm_train/llm_train.py:408-487](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L408-L487)：构造 `command = ['swift', cmd, '--k', 'v', ...]`，叠加 `--add_version False --output_dir ... --logging_dir ... --ignore_args_error True`；按 `use_ddp` 设 `NPROC_PER_NODE`，按 GPU 列表设 `CUDA_VISIBLE_DEVICES`（NPU 走 `ASCEND_RT_VISIBLE_DEVICES`）；最终 `run_command` 为 `nohup swift ... > run.log 2>&1 &`（非 Windows）。

提交按钮调 `train_local` 后台启动并刷新任务列表：

- [swift/ui/llm_train/llm_train.py:297-303](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L297-L303)：`submit.click(cls.train_local, list(cls.valid_elements().values()), [...])`。
- [swift/ui/llm_train/llm_train.py:517-539](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L517-L539)：`train_local`，非 dry-run 时 `run_command_in_background_with_popen(command, all_envs, log_file)`，并 `Runtime.refresh_tasks` 更新运行中任务。
- [swift/ui/llm_train/llm_train.py:496-515](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L496-L515)：`train_studio`，dry-run 模式只生成命令不执行（对应 UI 上「仅生成运行命令」勾选）。

`swift app` 的 `SwiftApp.run`：自动部署 + 对话界面：

- [swift/pipelines/app/app.py:16-39](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/app/app.py#L16-L39)：`SwiftApp` 继承 `SwiftPipeline`，`args_class = AppArguments`；L22 `deploy_context = nullcontext() if args.base_url else run_deploy(args, return_url=True)`——有 `base_url` 直连外部服务，否则自拉起；L25 `build_ui(...)` 构建聊天界面；L33-37 按 `infer_backend` 选并发度。

`AppArguments` 继承 `WebUIArguments + DeployArguments`，是「部署参数 + 一层 UI 壳」：

- [swift/arguments/app_args.py:14-39](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/app_args.py#L14-L39)：`AppArguments` 多继承；`base_url` 为空时走本地部署；`__post_init__` 调 `DeployArguments.__post_init__` 并用 `find_free_port` 选端口、按 `model_meta` 推断 `system`/`is_multimodal`。

`build_ui` 对话界面用 `InferClient` 跑流式聊天：

- [swift/pipelines/app/build_ui.py:96-137](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/app/build_ui.py#L96-L137)：`build_ui` 函数，L104 `client = InferClient(base_url=base_url)`，L107-136 用 `gr.Blocks` 摆 System/Chatbot/Input/上传/提交/重生成/清空 按钮，L125 `model_chat_ = partial(model_chat, client=client, ...)`。
- [swift/pipelines/app/build_ui.py:53-74](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/app/build_ui.py#L53-L74)：`model_chat`，调 `client.infer_async(InferRequest(messages=messages), request_config=...)`；流式则 `async for resp in resp_or_gen` 累加 `delta.content`，非流式直接取 `choices[0].message.content`——这正是 u6-l1 的 OpenAI 兼容协议在 UI 侧的消费。

#### 4.4.4 代码实践

1. **实践目标**：用 dry-run 模式验证「表单 → `swift` 命令」的映射，再体验 `swift app` 对话。
2. **操作步骤**：
   - 启动 `swift web-ui`，进入训练 Tab，填入模型 `Qwen/Qwen2.5-7B-Instruct`、数据集 `swift/self-cognition#500`、`tuner_type=lora`，**勾选「仅生成运行命令」**，点「开始训练」。
   - 复制弹出的运行命令，与 [swift/ui/llm_train/llm_train.py:408-444](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L408-L444) 的拼接逻辑逐段对照（`--model`/`--dataset`/`--tuner_type`/`--output_dir`/`--ignore_args_error True`）。
   - 取消勾选 dry-run，选一张 GPU 真实提交，在 Runtime 面板查看 `run.log` 与「运行中任务」。
   - 另开终端执行 `swift app --model Qwen/Qwen2.5-7B-Instruct --infer_backend vllm`（或 transformers），等其拉起 deploy 后在浏览器聊天。
3. **需要观察的现象**：dry-run 命令只包含「与默认值不同的字段」；真实提交后 `run.log` 出现 `swift sft ...` 的 `run sh:` 行；`swift app` 先打印部署就绪日志再开聊。
4. **预期结果**：dry-run 命令与手写 CLI 等价；`swift app` 的流式输出逐字出现。
5. 若无 GPU，dry-run 部分仍可完成（不依赖硬件），真实训练与 `swift app` 的运行效果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`LLMTrain.train` 为什么要「只保留与默认值不同的字段」再拼命令？

**参考答案**：为了让生成的命令简洁、可读、可复用——默认值不写进命令也能生效，命令只体现用户的「意图差异」。同时它用 `RLHFArguments(**kwargs)` 实例化一次以拿到规范化的 `output_dir`/`logging_dir`，保证路径命名一致。

**练习 2**：`swift app` 在 `--base_url` 有值和无值时行为有何不同？

**参考答案**：有 `--base_url` 时，`SwiftApp` 用 `nullcontext()` 跳过部署、直接连外部 OpenAI 兼容服务；无 `--base_url` 时，用 `run_deploy(args, return_url=True)` 拉起一个临时 `swift deploy` 子进程作为上下文管理器，`build_ui` 连本地服务，退出 `with` 即终止部署。

**练习 3**：`swift app` 的并发度为何 transformers 后端是 1、加速后端是 16？

**参考答案**：transformers 后端单条推理占用整个模型且不支持高并发批处理，并发 >1 易阻塞；vllm/sglang/lmdeploy 等加速后端原生支持连续批处理，可安全开高并发。见 [swift/pipelines/app/app.py:33-37](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/app/app.py#L33-L37)。

## 5. 综合实践

**任务**：从「改一个参数」到「看到它在命令里」走一遍全链路，把本讲的元数据驱动、`build_ui`、表单→命令三件事串起来。

1. 在 `swift/ui/llm_train/llm_train.py` 的 `do_build_ui` 里找到 `elem_id='seed'` 那一行（[L255](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L255)），确认 `seed` 是 `RLHFArguments` 的字段。
2. 启动 `swift web-ui`，在训练 Tab 把 `seed` 改成一个非默认值（如 `42`），同时勾选「仅生成运行命令」并提交。
3. 在生成的命令里找到 `--seed 42`，对照 [swift/ui/llm_train/llm_train.py:339-354](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L339-L354) 解释：`seed` 为何会进入 `kwargs`（与默认值不同），又为何在 [L415-425](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/ui/llm_train/llm_train.py#L415-L425) 被拼成 `--seed 42`。
4. 把 `seed` 改回默认值再提交，观察命令里 `--seed` 是否消失，验证「只留差异字段」机制。
5. （可选）启动 `swift app --model <上一步训练产出的 checkpoint> --infer_backend transformers`，在聊天界面验证微调效果，体会「web-ui 训练 → app 试效果」的闭环。

> 说明：步骤 2-4 不依赖 GPU（dry-run 只生成命令），可在任何环境完成；步骤 5 需要可推理的硬件，**待本地验证**。

## 6. 本讲小结

- ms-swift 有两条 Gradio 入口：`swift web-ui` 是覆盖训练/RLHF/GRPO/推理/导出/评测/采样的**全功能控制台**，`swift app` 是基于已部署模型的**对话试用台**；二者都不走 torchrun 多进程。
- `BaseUI` 用「`elem_id` = 参数名」+ 对 Gradio 组件 `__init__` 的 `update_data` 猴子补丁，让表单字段从 `RLHFArguments` 等 dataclass **单一来源**自动获得候选值/默认值/文案，避免 UI 与参数定义漂移。
- `build_ui` 用模板方法模式编排：设全局 `builder`/`base_builder` → `do_build_ui` 布局 → 递归 `sub_ui` → 根 Tab 时绑 `after_build_ui` 跨组件回调。
- `update_input_model` 实现「模型路径 → 表单回填」：本地 checkpoint 走 `args.json` 全字段回填，Hub 模型仅回填 `template`/`model_type`/`system`，并按模型缓存历史训练记录。
- `web-ui` 提交时由 `LLMTrain.train` 把表单值翻译成一条 `swift sft/rlhf ...` 命令 + 环境变量，以子进程后台启动，UI 与训练解耦且「所见即所执」。
- `swift app` = `run_deploy`（自动拉起 OpenAI 兼容服务）+ `build_ui`（用 `InferClient` 跑流式聊天），是 u8-l2 部署能力的前端壳。

## 7. 下一步学习建议

- **回头精读一个 Tab 的完整实现**：选 `swift/ui/llm_infer/llm_infer.py`，对照本讲的 `LLMTrain` 看推理 Tab 如何复用 `DeployArguments` 与 `InferClient`，巩固「元数据驱动」范式。
- **扩展到自定义 UI**：若要加一个业务专属面板，可继承 `BaseUI`、把 `choice_dict`/`default_dict`/`arguments` 指向自己的 dataclass、实现 `do_build_ui`，并在 `SwiftWebUI.run()` 的 `gr.Tabs()` 里加一行 `XXX.build_ui(XXX)`；这与 u10-l3 的「自定义注册」思路一致。
- **串联部署与评测**：本讲的 `swift app` 依赖 u8-l2 的 `run_deploy`，`LLMEval` Tab 依赖 u8-3 的临时部署评测；建议接下来重读 u8 单元，把「UI 触发 → 部署 → 评测」的端到端链路在脑中闭环。
- **阅读源码顺序建议**：`swift/ui/base.py`（机制）→ `swift/ui/llm_train/llm_train.py`（最复杂的 Tab）→ `swift/ui/llm_train/model.py`（最简子面板）→ `swift/pipelines/app/build_ui.py`（轻量对话界面）。
