# u8-l4 Gradio 界面与二次开发实践

## 1. 本讲目标

本讲是学习手册的收官之讲。学完后你应该能够:

1. 说清 `gradio_app.py` 的**双进程架构**:Gradio 前端进程如何通过 `subprocess.Popen` 拉起 `launch.py --gradio` 训练子进程,两者之间没有任何函数调用,全靠**三个文件**(`progress`、`logs`、`alive`)通信。
2. 解释 `--gradio` 开关在 `launch.py` 里引发的四处行为变化(无色日志、日志落盘、ProgressCallback 替换进度条、训练后自动导出)。
3. 理解 watcher 看门狗进程如何用 `alive` 心跳文件的「最后活跃时间戳」实现孤儿进程清理。
4. 理解 `CodeSnapshotCallback` / `ConfigSnapshotCallback` 如何用 `git ls-files` 与 yaml 快照保障试验可复现。
5. 独立完成一个**自定义扩展组件**(以新 exporter 为例):写出注册骨架、通过 `custom_import` 注入、在配置中切换并验证产物。

本讲也是对 u3-l1 注册机制、u8-l3 网格导出两讲的最终落地。

## 2. 前置知识

- **Gradio**:一个用几行 Python 就能把函数包装成网页界面的库。核心概念是 `gr.Blocks`(页面布局容器)与事件绑定(`btn.click(fn=..., inputs=..., outputs=...)`)。绑定到按钮的函数若写成 `yield` 生成器,前端会持续刷新输出——本讲的 `run()` 正是生成器。
- **子进程(subprocess)**:`subprocess.Popen` 启动一个独立的操作系统进程。它与父进程不共享 Python 状态,只能通过命令行参数、文件、信号等方式交换信息。Gradio 进程与训练进程就是这种「松耦合」关系:训练崩了前端不会崩,前端关了训练还能靠看门狗收尾。
- **心跳文件与看门狗(watchdog)**:一个进程周期性地把当前时间戳写进某个文件,另一个「看门狗」进程定期检查该文件——若时间戳过旧,就判定进程失联并将其杀死。这是防止 GPU 上残留孤儿任务的最朴素方案。
- **SIGKILL**:不可被捕获、阻塞或忽略的强制终止信号(`os.kill(pid, signal.SIGKILL)`),进程没有机会做清理,相当于「直接拔电源」。
- **`git ls-files`**:列出 git 索引中的所有受控文件。`--others --exclude-standard` 列出未被 git 跟踪但也没被 `.gitignore` 忽略的新文件。
- **回调(Callback)**:PyTorch Lightning 的 `Callback` 对象在训练各生命周期节点(`on_fit_start`、`on_train_batch_end`……)被自动调用,是往训练循环里「埋钩子」的标准方式(u2-l4 已接触)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [gradio_app.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py) | Gradio 网页前端:模型配置加载、训练子进程启动、状态轮询、看门狗。一个文件承载 `launch`(起界面)与 `watch`(起看门狗)两种运行模式 |
| [launch.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py) | 训练总入口;`--gradio` 开关改变日志、回调与导出行为 |
| [threestudio/utils/callbacks.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py) | 四个自定义回调:代码快照、配置快照、自定义进度条、Gradio 进度文件写入 |
| [threestudio/__init__.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py) | 注册表本体:`__modules__` 字典 + `register`/`find`,二次开发的接入点 |
| [threestudio/systems/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py) | `on_predict_start`/`on_predict_epoch_end`:exporter 扩展的消费现场 |
| [threestudio/models/exporters/base.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py) | `Exporter` 基类、`ExporterOutput` 数据契约,以及官方自带的 `dummy-exporter` 参考实现 |

> ⚠️ **重要事实**:本仓库的 `configs/` 下**没有** `gradio/` 子目录(只有 dreamcraft3d 四份阶段配置),而 `gradio_app.py` 引用了 `configs/gradio/dreamfusion-if.yaml` 等 6 个文件——这是从上游 threestudio 仓库继承下来、但配置文件未随仓库发布的代码。直接 `python gradio_app.py` 会在构建界面时抛 `FileNotFoundError`(详见 4.1.3)。本讲会把「修复它」设计进实践任务。

## 4. 核心概念与源码讲解

### 4.1 gradio_app.py 总览:双进程架构与前端组装

#### 4.1.1 概念说明

`gradio_app.py` 是从上游 threestudio 原样继承的网页 demo 入口。它的架构是**三个进程**:

```
┌─────────────────┐  Popen #1   ┌──────────────────────┐
│  Gradio 前端进程  │ ──────────► │  launch.py --gradio   │
│ (python          │             │  (训练子进程, 占 GPU)  │
│  gradio_app.py)  │  Popen #2   ├──────────────────────┤
│                  │ ──────────► │  gradio_app.py watch  │
│  每秒轮询三文件    │             │  (看门狗子进程)        │
└─────────────────┘             └──────────────────────┘
        ▲                              │
        └──── 文件系统:progress / logs / alive / save/ ────┘
```

前端进程自己不 import torch、不碰 GPU;训练进程完全不知道 Gradio 的存在。两者唯一的「接口」是试验目录下的几个普通文件。这种设计让界面崩溃不会拖垮训练,也让同一个 `launch.py` 同时服务命令行与网页两种用法。

#### 4.1.2 核心流程

以 `python gradio_app.py`(默认 `launch` 模式)启动后的时间线:

1. 构建界面:读取 `model_config` 表,渲染下拉框 / 提示词框 / 滑杆 / 配置查看器。
2. 用户点 Run → 触发生成器 `run(...)`。
3. `run` 把编辑器里的 yaml 写入临时文件,计算 `name`/`tag`,拼出试验目录。
4. `Popen` 拉起训练子进程(`launch.py --config 临时文件 --train --gpu 0 --gradio ...`)。
5. `Popen` 拉起看门狗子进程(`gradio_app.py watch --pid ... --trial-dir ...`)。
6. 训练进行期间,`run` 每秒 `yield` 一次最新状态,Gradio 据此刷新页面。
7. 用户点 Stop → `stop_run` 向训练进程发 `SIGKILL`,生成器被取消,按钮进入 Reset 态。

#### 4.1.3 源码精读

先看模型配置表——前端下拉框的全部可选模型,以及它指向的配置文件:

```python
EXP_ROOT_DIR = "outputs-gradio"
DEFAULT_PROMPT = "a delicious hamburger"
model_config = [
    ("DreamFusion (DeepFloyd-IF)", "configs/gradio/dreamfusion-if.yaml"),
    ...
]
model_name_to_config = {m[0]: m[1] for m in model_config}
```

[gradio_app.py:L72-L83](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L72-L83) 定义了 6 个上游 threestudio 的文生 3D 模型选项(**没有 DreamCraft3D 本身的配置**),并规定所有试验输出到 `outputs-gradio/` 而非 `outputs/`。

前端在构建滑杆默认值时会真的去解析这些 yaml:

```python
def load_model_config_attrs(model_name):
    config_str = load_model_config(model_name)   # open(...).read()
    from threestudio.utils.config import load_config
    cfg = load_config(
        config_str,
        cli_args=[
            "name=dummy", "tag=dummy", "use_timestamp=false",
            f"exp_root_dir={EXP_ROOT_DIR}",
            "system.prompt_processor.prompt=placeholder",
        ],
        from_string=True,
    )
    return {
        "source": config_str,
        "guidance_scale": cfg.system.guidance.guidance_scale,
        "max_steps": cfg.trainer.max_steps,
    }
```

[gradio_app.py:L90-L109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L90-L109) 复用了 u2-l2 讲过的 `load_config`,以 `from_string=True` 直接解析 yaml 字符串,再用一组 dummy 命令行参数把必填字段(`name`/`tag`/`prompt`)补齐到不报错的最低限度,最后只抽取 `guidance_scale` 与 `max_steps` 两个字段用于初始化滑杆。注意它传的是**硬编码的命令行覆盖**——这正说明命令行 extras 的优先级高于 yaml,与 u2-l2 的结论一致。

界面骨架与事件绑定:

```python
model_selector = gr.Dropdown(value=model_choices[0], choices=model_choices, ...)
guidance_scale_input = gr.Slider(
    ..., value=load_model_config_attrs(model_selector.value)["guidance_scale"], ...
)
...
model_selector.change(
    fn=on_model_selector_change,
    inputs=model_selector,
    outputs=[config_editor, guidance_scale_input],
)
run_event = run_btn.click(fn=run, inputs=[...], outputs=[pid, status, logs, ...])
stop_btn.click(fn=stop_run, inputs=[pid], outputs=[run_btn, stop_btn], cancels=[run_event])
```

[gradio_app.py:L283-L379](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L283-L379) 组装了页面:左列是输入(模型下拉、prompt、guidance_scale、seed、max_steps、只读的完整配置查看器),右列是输出(日志、验证图、测试视频、3D 网格)。两个关键细节:

- `guidance_scale_input` 的初值在**构建 Blocks 时**就调用了一次 `load_model_config_attrs`([L296](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L296))——这就是直接启动必然 `FileNotFoundError` 的位置;
- `stop_btn.click(..., cancels=[run_event])`([L377-L379](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L377-L379))——Gradio 会取消正在挂起的 `run` 生成器,这一点是理解 4.3 心跳机制的前提。

命令行入口用位置参数区分两种角色:

```python
parser.add_argument("operation", type=str, choices=["launch", "watch"])
```

[gradio_app.py:L428-L450](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L428-L450):`launch` 模式带 `--port`(默认 7860)与 `--listen`;`watch` 模式带 `--pid`/`--trial-dir` 及三个超时参数。同一个文件、同一个 `__main__`,靠第一个位置参数分流。

#### 4.1.4 代码实践

1. **实践目标**:确认 `configs/gradio/` 缺失的影响,并把前端改造成指向 DreamCraft3D 的 coarse 配置。
2. **操作步骤**:
   - 在仓库根目录执行 `ls configs/`,确认只有四份 dreamcraft3d yaml,没有 `gradio/` 子目录。
   - 复制一份粗阶段配置作为前端入口(注意 Gradio demo 只暴露 `prompt`/`guidance_scale`/`seed`/`max_steps` 四个旋钮,而 dreamcraft3d 粗阶段还需要 `data.image_path`,所以要么在配置里写死路径,要么接受 `???` 必填缺失报错并把它补进 dummy cli_args)。
   - 按下文「示例代码」修改 `model_config` 表(这是**前端数据表**,不违反「不改源码」精神之外的本讲约束;若你不想动 `gradio_app.py`,也可以从上游 threestudio 仓库把 `configs/gradio/*.yaml` 六份文件拷回来,那样一行代码都不用改):

     ```python
     # 示例代码:替换 gradio_app.py 中的 model_config(位置约在 L74)
     model_config = [
         ("DreamCraft3D (coarse)", "configs/gradio/dreamcraft3d-coarse.yaml"),
     ]
     ```

     同时把 [L94-L104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L94-L104) 的 dummy cli_args 里补上 `data.image_path=load/images/hamburger_rgba.png` 一类覆盖,避免配置解析在 `???` 上失败。
   - 运行 `python gradio_app.py`,浏览器打开 `http://127.0.0.1:7860`。
3. **需要观察的现象**:改造前启动会在终端立刻出现 `FileNotFoundError: [Errno 2] No such file or directory: 'configs/gradio/dreamfusion-if.yaml'`;改造后页面正常渲染,下拉框只剩一个选项,配置查看器里显示 coarse 阶段 yaml 全文。
4. **预期结果**:界面可用;若不改造而直接拷入上游 6 份配置,还需保证对应 guidance 权重(如 DeepFloyd IF)可下载。
5. 完整训练能否跑通依赖 GPU 与权重,**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `load_model_config_attrs` 要传 `name=dummy tag=dummy use_timestamp=false` 这些覆盖?
**答案**:u2-l2 讲过,`ExperimentConfig` 中 `name`/`tag` 是必填的 `???`,且 `__post_init__` 会用它们创建试验目录(副作用)。前端这里只想读取 `guidance_scale` 和 `max_steps`,并不真要训练,所以用 dummy 值把配置「解析通过」即可;`use_timestamp=false` 则避免每读一次配置就多建一个带时间戳的目录。

**练习 2**:`gradio_app.py` 为什么把 `launch` 和 `watch` 两个角色写进同一个文件?
**答案**:`watch` 是被 `run()` 用 `python gradio_app.py watch ...` 再次拉起的(见 4.3.3 的 [gradio_app.py:L225-L228](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L225-L228))。单文件部署意味着使用者只需拷一个脚本;同时 `watch` 复用了文件顶部的 `psutil`/`signal` 导入。这是「工具 + 守护」打包成一个入口的常见工程手法。

### 4.2 run():从前端参数到 launch.py 子进程

#### 4.2.1 概念说明

`run()` 是前端与训练的「翻译官」:把 6 个界面控件值翻译成一条 `launch.py` 命令行。它体现了 threestudio 配置系统的核心信条——**一切界面旋钮最终都变成命令行 extras 覆盖**(u2-l2 的点号语法)。前端编辑器里那份可编辑(实际 `interactive=False`,只读)的 yaml 则被写到临时文件,作为 `--config` 传入。

#### 4.2.2 核心流程

```伪代码
run(model_name, config, prompt, guidance_scale, seed, max_steps):
    把 config 字符串写入临时文件
    name  = 模型配置文件的主文件名(如 dreamcraft3d-coarse)
    tag   = "@%Y%m%d-%H%M%S"(自己带时间戳)
    trial_dir = outputs-gradio/<name>/<tag>

    Popen("python launch.py --config <临时文件> --train --gpu 0 --gradio
           trainer.enable_progress_bar=false
           name=<name> tag=<tag> exp_root_dir=outputs-gradio use_timestamp=false
           system.prompt_processor.prompt=<prompt>
           system.guidance.guidance_scale=<gs> seed=<seed> trainer.max_steps=<steps>")

    Popen("python gradio_app.py watch --pid <训练pid> --trial-dir <trial_dir>")

    while 训练进程存活:
        sleep(1 秒)
        yield [状态六元组, 隐藏 Run 按钮, 显示 Stop 按钮]

    等两个子进程退出
    yield 最终状态(progress 置为 "Finished."),按钮切回 Run
```

注意三个设计:配置**内容**走临时文件、配置**覆盖**走命令行;`tag` 自带 `@时间戳` 但又传 `use_timestamp=false`,时间戳只出现一次;训练命令硬编码 `--gpu 0`。

#### 4.2.3 源码精读

```python
config_file = tempfile.NamedTemporaryFile()
with open(config_file.name, "w") as f:
    f.write(config)

name = os.path.basename(model_name_to_config[model_name]).split(".")[0]
tag = datetime.now().strftime("@%Y%m%d-%H%M%S")
trial_dir = os.path.join(EXP_ROOT_DIR, name, tag)
alive_path = os.path.join(trial_dir, "alive")
```

[gradio_app.py:L197-L207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L197-L207):前端自己决定试验目录布局,与训练进程「约好」目录,而不是等训练进程回报。`alive_path` 在 4.3 讲。

```python
process = subprocess.Popen(
    f"python launch.py --config {config_file.name} --train --gpu 0 --gradio trainer.enable_progress_bar=false".split()
    + [
        f'name="{name}"',
        f'tag="{tag}"',
        f"exp_root_dir={EXP_ROOT_DIR}",
        "use_timestamp=false",
        f'system.prompt_processor.prompt="{prompt}"',
        f"system.guidance.guidance_scale={guidance_scale}",
        f"seed={seed}",
        f"trainer.max_steps={max_steps}",
    ]
)
```

[gradio_app.py:L209-L222](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L209-L222) 是本讲的「接线核心」:u1-l4 的五种运行模式里,Gradio 选择 `--train` + `--gradio` 组合;`trainer.enable_progress_bar=false` 关掉 TQDM(否则日志文件里会充满刷新字符);随后 8 个 extras 覆盖逐项落地——`name`/`tag`/`exp_root_dir`/`use_timestamp` 四个保证训练进程写进**前端算好的同一个 trial_dir**,后四个把界面旋钮注入配置。对照 u2-l2:`load_config` 的合并优先级是 yaml < 命令行 extras < Python kwargs(`n_gpus`),因此这些覆盖必然生效。

```python
while process.poll() is None:
    time.sleep(status_update_interval)
    yield get_current_status(process, trial_dir, alive_path).tolist() + [
        gr.update(visible=False),
        gr.update(value="Stop", variant="stop", visible=True),
    ]
process.wait()
watch_process.wait()
```

[gradio_app.py:L232-L241](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L232-L241):`process.poll()` 非阻塞探测子进程是否退出,循环体每秒 `yield` 一次;由于 `run` 是生成器,Gradio 每收到一个值就刷新一次全部 outputs(包括按钮可见性)。训练结束后先 `wait()` 收尸,再 `yield` 终态([L243-L250](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L243-L250)),progress 字段被硬编码为 `Finished.`。

再看 `launch.py` 侧如何「接住」`--gradio`。开关在参数解析处注册:

```python
parser.add_argument("--gradio", action="store_true", help="if true, run in gradio mode")
```

[launch.py:L231-L233](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L231-L233)。进入 `main()` 后共影响四处,第一处是日志格式:

```python
for handler in logger.handlers:
    if handler.stream == sys.stderr:
        if not args.gradio:
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            handler.addFilter(ColoredFilter())
        else:
            handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
```

[launch.py:L90-L96](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L90-L96):命令行模式给日志上色(`ColoredFilter`,[L9-L40](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L9-L40));Gradio 模式则**禁用颜色**——因为日志即将写入文件,ANSI 转义序列混进文件会污染 `tail` 的展示。

第二处,日志落盘到试验目录:

```python
if args.gradio:
    fh = logging.FileHandler(os.path.join(cfg.trial_dir, "logs"))
    fh.setLevel(logging.INFO)
    ...
    logger.addHandler(fh)
```

[launch.py:L125-L131](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L125-L131):这就是三文件协议里 `logs` 的生产者。

第三处,回调替换:

```python
if args.gradio:
    callbacks += [ProgressCallback(save_path=os.path.join(cfg.trial_dir, "progress"))]
else:
    callbacks += [CustomProgressBar(refresh_rate=1)]
```

[launch.py:L150-L155](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L150-L155):命令行模式挂 TQDM 进度条给终端用户看;Gradio 模式改为 `ProgressCallback` 把进度百分比写进文件给网页看。**同一信息,两种受众,两个回调**。

第四处,训练结束后自动导出:

```python
if args.train:
    trainer.fit(system, datamodule=dm, ckpt_path=cfg.resume)
    trainer.test(system, datamodule=dm)
    if args.gradio:
        # also export assets if in gradio mode
        trainer.predict(system, datamodule=dm)
```

[launch.py:L194-L199](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L194-L199):命令行的 `--train` 只做 fit + test;Gradio 模式追加 `trainer.predict`——正是 u2-l4/u8-l3 讲过的 predict 钩子路径(`on_predict_start` 构建 exporter、`on_predict_epoch_end` 导出 obj),这让网页用户训练完直接拿到 3D 网格展示在 `gr.Model3D` 组件里。

最后,入口处还有一个未完成的尝试:

```python
if args.gradio:
    # FIXME: no effect, stdout is not captured
    with contextlib.redirect_stdout(sys.stderr):
        main(args, extras)
```

[launch.py:L247-L252](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L247-L252):作者想把 stdout 重定向到 stderr(可能为了让 print 输出也被日志系统捕获),但注释自认无效。读源码时要能识别这类「诚实的 FIXME」——它不是功能,是历史痕迹。

#### 4.2.4 代码实践

1. **实践目标**:不打开网页,手工执行一条与 `run()` 完全等价的命令,验证命令拼接正确。
2. **操作步骤**(无需 GPU 也能验证到「配置解析通过」那一步):
   - 仿照 [L211-L221](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L211-L221) 在终端执行(**示例命令**,`<cfg>` 换成某份真实配置路径):

     ```bash
     python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml \
       --train --gpu 0 --gradio trainer.enable_progress_bar=false \
       name=gradio-test tag="@manual" exp_root_dir=outputs-gradio \
       use_timestamp=false seed=0 trainer.max_steps=10 \
       system.prompt_processor.prompt="a delicious hamburger"
     ```
   - 观察输出目录 `outputs-gradio/gradio-test/@manual/` 是否生成。
3. **需要观察的现象**:`trial_dir` 布局与命令行 `outputs/` 完全同构(ckpts/save/code/configs/tb_logs/csv_logs),另**多出** `logs` 与 `progress` 两个文件;终端没有 TQDM 进度条、没有彩色日志。
4. **预期结果**:前 10 步训练后自动跑 test 与 predict,`save/` 下出现 `it*-test.mp4` 与 `it*-export/` 目录(u2-l4 讲过的产物命名)。
5. 真实训练需要 ≥20GB 显存与全部预训练权重,**待本地验证**;无 GPU 环境下可以用 `trainer.max_steps=1` 观察 OmegaConf 解析与目录创建行为后在加载权重前中止。

#### 4.2.5 小练习与答案

**练习 1**:为什么前端传 `use_timestamp=false` 又自己构造带时间戳的 `tag`?
**答案**:`__post_init__` 生成 trial_dir 的规则是 `exp_root_dir/name/tag[@时间戳]`(u2-l4)。时间戳后缀由配置系统随机生成,前端无法预知;而前端必须在 `Popen` **之前**就知道 trial_dir(用于构造 `alive_path` 与传给 watcher)。所以前端自己生成时间戳化的 tag、再关掉配置系统的自动时间戳,双方就指向同一个目录。

**练习 2**:`--gradio` 模式为什么必须 `trainer.enable_progress_bar=false`?
**答案**:TQDM 进度条依赖 `\r` 回车刷新单行,无人观看时这些控制字符会堆积在缓冲区;更重要的是 Gradio 模式的进度展示已经改由 `ProgressCallback` 写 `progress` 文件承担,TQDM 纯属冗余。

**练习 3**:Gradio 模式下 `trainer.predict` 没有传 `ckpt_path`,导出用的是什么权重?
**答案**:用的是**内存中当前的 system 对象**——`fit` 刚结束,模型参数还在,`predict` 紧随其后复用同一实例。这与命令行 `--export` 模式不同:后者是新进程,必须靠 `resume=` 从磁盘恢复权重(u2-l4)。

### 4.3 进度文件、日志与 alive 心跳:watcher 看门狗

#### 4.3.1 概念说明

三文件协议的「生产—消费」关系:

| 文件 | 生产者 | 消费者 | 内容 |
| --- | --- | --- | --- |
| `progress` | 训练进程(`ProgressCallback`) | 前端每秒读取 | 单行进度文本,如 `Generation progress: 12.34%` |
| `logs` | 训练进程(`FileHandler`) | 前端每秒 `tail` 末 10 行 | 训练日志 |
| `alive` | **前端**(每次轮询时写入) | watcher 看门狗 | 当前 Unix 时间戳 |

`alive` 的方向最反直觉:**不是训练进程报告自己活着,而是前端报告「我还在看着你」**。如果用户关闭浏览器页签、Gradio 队列取消 `run` 生成器,前端就不再写 `alive`;训练本身可能在 GPU 上跑几小时,于是 watcher 检测到 `alive` 时间戳超过 10 秒未更新,直接 `SIGKILL` 训练进程。这防止了「界面没了、GPU 还在空烧」的孤儿任务。

#### 4.3.2 核心流程

```
前端每秒:                      watcher 每 check_interval(1s):
  写 time.time() 到 alive        若 alive 文件不存在 → 继续等(受 wait_timeout 总时限)
  读 progress 全文               若训练 pid 不存在 → 退出(正常结束)
  tail logs 末 10 行             若 time.time() - alive时间戳 > alive_timeout(10s)
  glob save/ 最新 png/mp4/obj        → SIGKILL 训练进程, 退出
  yield 给页面
```

#### 4.3.3 源码精读

前端侧的状态聚合函数:

```python
if os.path.exists(os.path.dirname(alive_path)):
    alive_fp = open(alive_path, "w")
    alive_fp.seek(0)
    alive_fp.write(str(time.time()))
    alive_fp.flush()
```

[gradio_app.py:L122-L128](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L122-L128):心跳写入。注意守卫条件是「trial_dir 已存在」——训练进程启动初期目录可能还没建好,此时跳过写心跳,而 watcher 那边也还没找到 `alive` 文件,两侧逻辑对齐。

```python
log_path = os.path.join(trial_dir, "logs")
progress_path = os.path.join(trial_dir, "progress")
...
if os.path.exists(progress_path):
    status.progress = open(progress_path).read()
else:
    status.progress = "Setting up everything ..."
```

[gradio_app.py:L130-L145](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L130-L145):progress 缺失时显示 `Setting up everything ...`,覆盖了权重下载、数据准备等「还没进训练循环」的阶段。`tail(open(log_path, "rb"), window=10)` 用二进制模式读末 10 行——`tail` 函数([L19-L49](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L19-L49))从文件末尾按 1KB 块倒着读,避免加载整个日志文件,这是日志工具的标准技巧(类似 `tail -n`)。

产物检索用命名约定反查步数:

```python
images = glob.glob(os.path.join(save_path, "*.png"))
steps = [int(re.match(r"it(\d+)-0\.png", os.path.basename(f)).group(1)) for f in images]
images = sorted(list(zip(images, steps)), key=lambda x: x[1])
if len(images) > 0:
    status.output_image = images[-1][0]
```

[gradio_app.py:L148-L181](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L148-L181):从 `save/*.png`、`save/*.mp4`、`save/*export/` 三类文件名中用正则抽出 `it(\d+)` 步数,取最新者填入页面。这完全依赖 u2-l4 讲过的 SaverMixin 命名约定(`it{N}-0.png`、`it{N}-test.mp4`、`it{N}-export/`)——**文件名即接口**。末尾还有个 FIXME:gr.Model3D 加载不了手工保存的 obj,于是用 trimesh 重新导出到临时文件再喂给组件([L174-L181](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L174-L181))。

训练侧的 `ProgressCallback`:

```python
@rank_zero_only
def write(self, msg: str) -> None:
    self.file_handle.seek(0)
    self.file_handle.truncate()
    self.file_handle.write(msg)
    self.file_handle.flush()

@rank_zero_only
def on_train_batch_end(self, trainer, pl_module, *args, **kwargs):
    self.write(f"Generation progress: {pl_module.true_global_step / trainer.max_steps * 100:.2f}%")
```

[callbacks.py:L133-L156](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L133-L156):每次训练批次结束,`seek(0)+truncate` 把单行进度**原地覆写**——文件永远只有一行,前端 `open().read()` 拿到的就是最新值,不需要轮转。`on_validation_start`/`on_test_start`/`on_predict_start` 分别改写为渲染验证图 / 视频 / 导出网格的提示文本,与前端 `Setting up everything ...` 的兜底文案一起构成完整的进度叙事。分母是 `trainer.max_steps`,分子用 `true_global_step`(u3-l3 讲过的可信时间源)。

watcher 主体:

```python
def loop_find_progress_file():
    while True:
        if not os.path.exists(alive_path):
            time.sleep(check_interval)
        else:
            signal.alarm(0)
            return

def loop_check_alive():
    while True:
        if not psutil.pid_exists(pid):
            print(f"Process {pid} not exists, watcher exits.")
            exit(0)
        alive_timestamp = float(open(alive_path).read())
        if time.time() - alive_timestamp > alive_timeout:
            print(f"Alive timeout for process {pid}, killed.")
            try:
                os.kill(pid, signal.SIGKILL)
            except:
                print(f"Exception when killing process {pid}.")
            exit(0)
        time.sleep(check_interval)
```

[gradio_app.py:L399-L425](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L399-L425):两阶段循环。第一阶段等 `alive` 文件出现,用 `signal.alarm(wait_timeout)` 设总时限([L396-L397](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L396-L397),默认 10 秒;超时触发 `timeout_handler` 退出,防止训练进程卡死在启动阶段时看门狗永远空转);第二阶段每秒查 pid 存活与心跳新鲜度。三个默认参数在 [L440-L442](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/gradio_app.py#L440-L442) 定义:`alive_timeout=10`、`wait_timeout=10`、`check_interval=1`。

#### 4.3.4 代码实践

1. **实践目标**:亲手观察三文件协议的动态行为,特别是「前端停止轮询 → 10 秒后训练被杀」。
2. **操作步骤**(示例流程,`<pid>` 替换为真实值):
   - 按 4.2.4 启动一次短训练(或任意正在跑的 `--gradio` 训练),另开两个终端分别执行 `watch -n 1 'cat outputs-gradio/<name>/<tag>/progress'` 与 `watch -n 1 'cat outputs-gradio/<name>/<tag>/alive'`。
   - 若走网页流程:打开页面启动训练,观察 `alive` 每秒变化;然后**直接关闭浏览器页签**,再观察 `alive` 冻结、约 10 秒后训练进程消失(`ps -p <pid>`)、watcher 终端打印 `Alive timeout for process <pid>, killed.`
   - 若无网页,可手工模拟前端:起训练后用一个 shell 循环 `while true; do date +%s > .../alive; sleep 1; done`,Ctrl-C 停掉循环同样能触发看门狗。
3. **需要观察的现象**:`progress` 从 `Setting up everything ...`(此时文件还不存在,是前端兜底文案)变为 `Generation progress: x.xx%` 平滑递增;训练进入验证/测试/导出阶段时 `progress` 文本切换为对应提示。
4. **预期结果**:心跳停止后 10–11 秒内训练进程被 `SIGKILL`;若训练正常结束,watcher 打印 `Process <pid> not exists, watcher exits.` 后自行退出。
5. 需要 GPU 环境,**待本地验证**;无 GPU 时可只用两个终端 + 手工 touch/update 文件验证 `watch` 的判断逻辑。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `ProgressCallback.write` 要 `seek(0)` + `truncate()` 而不是 `open(path, "w")` 重开?
**答案**:每步重开文件要付出 open/close 系统调用开销,且句柄懒加载在 `file_handle` property 里只开一次;`seek(0)+truncate` 复用同一句柄把内容控制在一行内,读写双方都不需要处理多行历史。

**练习 2**:假设把 `alive_timeout` 调成 1 秒会有什么风险?
**答案**:前端轮询间隔 `status_update_interval` 也是 1 秒,加上 Gradio 队列调度延迟,正常情况下心跳间隔就可能逼近甚至超过 1 秒,看门狗会**误杀健康任务**。看门狗的超时必须显著大于心跳周期(工程上常取 3–10 倍),这也是默认值取 10 秒的原因。

**练习 3**:watcher 为什么先跑 `loop_find_progress_file` 再跑 `loop_check_alive`,而不是一上来就检查心跳?
**答案**:训练进程启动到前端第一次写 `alive` 之间有延迟(导入 torch、加载配置、创建目录),若直接检查会读到不存在的文件而崩溃。先用 `wait_timeout` 限定「等待 alive 出现」的总时长,`alive` 一出现就 `signal.alarm(0)` 取消总时限,再进入常态巡检——两阶段各自有独立的超时语义。

### 4.4 CodeSnapshot / ConfigSnapshot:可复现性的最后一块拼图

#### 4.4.1 概念说明

深度学习试验最大的敌人是「三个月后跑不出来了」。threestudio 的对策是把**当时的代码、当时的配置、当时的命令行**全部塞进试验目录:

- `code/`:整个仓库(除 `load/`)在训练开始瞬间的完整拷贝,含未提交的新文件;
- `configs/parsed.yaml` + `configs/raw.yaml`:合并后的最终配置与原始配置(u2-l2、u2-l4 已讲);
- `cmd.txt`:启动命令原文(由 [launch.py:L172-L177](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L172-L177) 的 `write_to_text` 写入 `sys.argv` 与解析后的 args)。

三者合起来,复现一次试验不需要 git 历史,只要 `cd code/ && 按cmd.txt执行` 即可。Gradio 模式同样享受这套保障(回调挂在 `--train` 分支下,与 `--gradio` 无关)。

#### 4.4.2 核心流程

```
on_fit_start (rank 0 only):
  CodeSnapshotCallback:
    git ls-files -- ":!:load/*"            → 受控文件, 排除权重目录
    git ls-files --others --exclude-standard → 未跟踪但未忽略的新文件
    两者求并集 → 逐文件 copyfile 到 code/ (保持目录结构)
  ConfigSnapshotCallback:
    dump_config(parsed.yaml)   ← OmegaConf 解析后的最终配置
    copyfile(raw.yaml)         ← 用户传入的原始 --config 文件
```

任一环节不在 git 仓库中时,代码快照失败只警告不中断训练。

#### 4.4.3 源码精读

```python
def get_file_list(self):
    return [
        b.decode()
        for b in set(
            subprocess.check_output('git ls-files -- ":!:load/*"', shell=True).splitlines()
        )
        | set(  # hard code, TODO: use config to exclude folders or files
            subprocess.check_output(
                "git ls-files --others --exclude-standard", shell=True
            ).splitlines()
        )
    ]
```

[callbacks.py:L64-L77](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L64-L77):两段 `git ls-files` 的并集。第一段的 pathspec 排除语法 `:!:load/*` 表示「排除 load/ 下所有文件」——权重目录动辄数 GB,不该进快照;第二段捞取**未跟踪**文件,意味着你刚写完还没 commit 的实验代码也会被快照(这正是科研场景最需要的)。TODO 注释承认排除规则是硬编码,留了配置化扩展点。

```python
@rank_zero_only
def save_code_snapshot(self):
    os.makedirs(self.savedir, exist_ok=True)
    for f in self.get_file_list():
        if not os.path.exists(f) or os.path.isdir(f):
            continue
        os.makedirs(os.path.join(self.savedir, os.path.dirname(f)), exist_ok=True)
        shutil.copyfile(f, os.path.join(self.savedir, f))

def on_fit_start(self, trainer, pl_module):
    try:
        self.save_code_snapshot()
    except:
        rank_zero_warn(
            "Code snapshot is not saved. Please make sure you have git installed and are in a git repository."
        )
```

[callbacks.py:L79-L94](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L79-L94):逐文件拷贝并保持相对目录结构;`@rank_zero_only` 保证多卡时只有主进程写一份;`try/except` 兜底「没有 git / 不在仓库」的场景——快照失败不阻断训练,只降级为警告。注意裸 `except` 会吞掉一切异常,是快照静默失败的潜在来源。

```python
@rank_zero_only
def save_config_snapshot(self):
    os.makedirs(self.savedir, exist_ok=True)
    dump_config(os.path.join(self.savedir, "parsed.yaml"), self.config)
    shutil.copyfile(self.config_path, os.path.join(self.savedir, "raw.yaml"))

def on_fit_start(self, trainer, pl_module):
    self.save_config_snapshot()
```

[callbacks.py:L103-L110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L103-L110):`parsed.yaml` 由 `dump_config` 序列化**合并后**的 `ExperimentConfig`(含命令行覆盖与时间戳,已 resolve),`raw.yaml` 原样复制用户传入的配置文件。两者对照可以精确还原「我改了哪些覆盖」。u2-l4 讲过的 `--export` 续接正是靠 `parsed.yaml`:它已固化 timestamp,重跑会写回同一 trial_dir。

两个回调都继承 `VersionedCallback`([callbacks.py:L19-L57](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L19-L57)),但 launch.py 里都以 `use_version=False` 实例化([launch.py:L140-L149](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L140-L149)),直接写入 trial_dir 根下的 `code/`、`configs/`,不产生 `version_N` 子目录——trial_dir 本身已含时间戳,再版本化是冗余。

#### 4.4.4 代码实践

1. **实践目标**:验证代码快照确实抓取了未提交的新文件,并理解其边界。
2. **操作步骤**:
   - 在仓库根目录新建一个文件 `my_scratch_note.txt`(内容随意),**不要** `git add`。
   - 按 4.2.4 跑一次 `max_steps=1` 的短训练(或任意一次 `--train` 运行)。
   - 训练启动后检查 `outputs-gradio/<name>/<tag>/code/my_scratch_note.txt` 是否存在;再检查 `code/load/` 是否存在;最后 `diff <(cat outputs-gradio/<name>/<tag>/configs/raw.yaml) configs/dreamcraft3d-coarse-nerf.yaml` 与 `grep timestamp outputs-gradio/<name>/<tag>/configs/parsed.yaml`。
3. **需要观察的现象**:未跟踪的 txt 被完整拷入 `code/`;`load/` 不在快照中;`raw.yaml` 与源配置逐字节一致;`parsed.yaml` 里出现命令行覆盖后的值(如 `max_steps: 1`)与固化的 tag。
4. **预期结果**:三者全部成立。若在非 git 目录(如把代码文件夹拷到别处)运行,终端出现 `Code snapshot is not saved...` 警告且训练继续。
5. 快照行为本身不需要 GPU——即使训练因缺权重失败,`on_fit_start` 也已经触发,**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:为什么排除规则要特别针对 `load/`,而不用 `.gitignore`?
**答案**:`.gitignore` 忽略的文件会被 `git ls-files --others --exclude-standard` 排除掉,但 `load/` 里的 tets、lights 等部分文件**是被 git 跟踪的**,`.gitignore` 管不到;pathspec 排除 `:!:load/*` 则作用于第一段查询的结果。更根本的是:即便某文件在 git 里,数 GB 的权重也不该进每次试验的快照。

**练习 2**:快照发生在 `on_fit_start`——训练中途修改源码会反映到快照吗?
**答案**:不会。`on_fit_start` 在 fit 开始时执行一次,`code/` 是那一瞬间的静态拷贝;之后的改动既不进快照也不影响已加载的 Python 模块(模块在进程启动时已 import)。这正是快照的意义:记录「训练真正运行的那份代码」。

**练习 3**:结合 u8-l3:`--export` 时为什么建议用 `parsed.yaml` 而不是 `raw.yaml`?
**答案**:`parsed.yaml` 保存了合并命令行覆盖、resolve 插值、固化 tag/timestamp 之后的最终配置;`raw.yaml` 可能含 `???` 必填缺失与未展开的插值。导出要重建与训练时完全一致的 system(经 `geometry_convert_from`/`resume` 接力),必须用最终形态的配置才能保证 trial_dir 一致、组件参数一致。

### 4.5 全项目扩展点地图与自定义扩展实战

#### 4.5.1 概念说明

八讲下来我们反复看到同一个模式:**配置里的 `X_type` 是注册名,`X` 段是构造参数,`threestudio.find(X_type)(cfg.X)` 产出对象**(u3-l1)。把这个模式反过来看,它就是一张「二次开发地图」——你想改项目的任何一环,就是在对应的孙包里加一个 `@threestudio.register` 类。注册表本体只有 13 行:

```python
__modules__ = {}

def register(name):
    def decorator(cls):
        __modules__[name] = cls
        return cls
    return decorator

def find(name):
    return __modules__[name]

...
from . import data, models, systems
```

[threestudio/__init__.py:L1-L13](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L1-L13) 定义注册/查找,末行 [L36](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/__init__.py#L36) 通过导入子包触发全量注册。

#### 4.5.2 核心流程:九类可插拔扩展点

| 扩展点 | 基类 / 契约 | 配置键 | 消费现场(find 的调用处) |
| --- | --- | --- | --- |
| system | `BaseSystem` / `BaseLift3DSystem` | `system_type` | launch.py L120 |
| data | datamodule(如 `SingleImageDataModule`) | `data_type` | launch.py L109 |
| geometry | `BaseImplicitGeometry` | `system.geometry_type` | `BaseLift3DSystem.configure` |
| material | `BaseMaterial` | `system.material_type` | 同上 |
| background | `BaseBackground` | `system.background_type` | 同上 |
| renderer | `BaseRenderer` | `system.renderer_type` | 同上 |
| guidance | 无强制基类(`BaseObject`/`BaseModule` 起步即可) | `system.guidance_type` / `guidance_3d_type` | dreamcraft3d.py configure |
| prompt_processor | `BasePromptProcessor` | `system.prompt_processor_type` | 同上 |
| exporter | `Exporter` + `ExporterOutput` | `system.exporter_type` | `BaseSystem.on_predict_start` |

唯一**不可**用配置插拔的是 callbacks——它们在 [launch.py:L133-L155](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L133-L155) 硬编码组装,新增回调必须改 launch.py。

两种接入方式:

1. **改仓库**:把新文件放进对应孙包(如 `threestudio/models/exporters/my_exporter.py`),孙包 `__init__.py` 会 import 它从而触发注册;
2. **零侵入**:`custom_import`——launch.py 在一切 `find` 之前执行:

```python
if len(cfg.custom_import) > 0:
    print(cfg.custom_import)
    for extension in cfg.custom_import:
        importlib.import_module(extension)
```

[launch.py:L102-L105](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L102-L105):import 的副作用执行你模块里的 `@threestudio.register`,之后所有 `find` 都能查到。u3-l1 的 vertical-gradient-background 与本讲实战都走这条路。

#### 4.5.3 源码精读:exporter 的接口契约

```python
@dataclass
class ExporterOutput:
    save_name: str
    save_type: str
    params: Dict[str, Any]


class Exporter(BaseObject):
    @dataclass
    class Config(BaseObject.Config):
        save_video: bool = False
    ...
    def __call__(self, *args, **kwargs) -> List[ExporterOutput]:
        raise NotImplementedError


@threestudio.register("dummy-exporter")
class DummyExporter(Exporter):
    def __call__(self, *args, **kwargs) -> List[ExporterOutput]:
        # DummyExporter does not export anything
        return []
```

[exporters/base.py:L11-L58](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/exporters/base.py#L11-L58):契约要点——继承 `Exporter`、实现 `__call__` 返回 `ExporterOutput` 列表;`configure(geometry, material, background)` 让你共享训练好的三大组件(u8-l3);`save_type` 必须对应 SaverMixin 的某个 `save_*` 方法。官方还留了一个 `dummy-exporter` 空实现当模板。

消费现场在系统基类:

```python
def on_predict_start(self) -> None:
    self.exporter: Exporter = threestudio.find(self.cfg.exporter_type)(
        self.cfg.exporter,
        geometry=self.geometry,
        material=self.material,
        background=self.background,
    )

def on_predict_epoch_end(self) -> None:
    ...
    exporter_output: List[ExporterOutput] = self.exporter()
    for out in exporter_output:
        save_func_name = f"save_{out.save_type}"
        if not hasattr(self, save_func_name):
            raise ValueError(f"{save_func_name} not supported by the SaverMixin")
        save_func = getattr(self, save_func_name)
        save_func(f"it{self.true_global_step}-export/{out.save_name}", **out.params)
```

[systems/base.py:L311-L332](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L311-L332):`save_type` 经字符串拼接反射到 `save_json` / `save_img` / `save_obj` 等方法([saving.py:L395-L648](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L395-L648) 提供了 `save_img_sequence`/`save_mesh`/`save_obj`/`save_json` 等),产物落到 `save/it{N}-export/{save_name}`——恰好是 4.3 讲 Gradio 前端 glob 的 `*export` 模式。也就是说:**自定义 exporter 的产物会自动出现在网页的 3D Mesh/图片面板里**,无需改前端。

作为对照,guidance 的契约更松:`__call__` 返回一个 dict,凡以 `loss_` 开头的键被系统自动加权汇总:

```python
guidance_out = self.guidance(guidance_inp, prompt_utils, **batch, ...)
for name, value in guidance_out.items():
    self.log(f"train/{name}", value)
    if name.startswith("loss_"):
        set_loss(name.split("_")[-1], value)
```

[dreamcraft3d.py:L196-L207](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L196-L207) 消费引导输出;[dreamcraft3d.py:L321-L329](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/dreamcraft3d.py#L321-L329) 完成计权:`loss_guidance_{name}` → 查 `cfg.loss["lambda_{name}"]`。所以新 guidance 只要返回 `{"loss_my": tensor}`,再在配置 loss 段加 `lambda_my: 1.0` 就接入了总损失。最简参考实现是 [clip_guidance.py:L13-L84](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/clip_guidance.py#L13-L84)(约 70 行,`BaseObject` 起步、`configure` 里加载模型、`__call__` 返回标量损失)。

#### 4.5.4 代码实践:实现 stats-exporter 扩展

1. **实践目标**:零侵入仓库,实现并挂载一个自定义 exporter,导出网格统计信息(JSON)。
2. **操作步骤**:
   - 在仓库根目录新建 `my_ext/` 目录,放入 `__init__.py`(空文件)与 `stats_exporter.py`:

     ```python
     # 示例代码:my_ext/stats_exporter.py
     from dataclasses import dataclass
     import threestudio
     from threestudio.models.exporters.base import Exporter, ExporterOutput
     from threestudio.utils.typing import *


     @threestudio.register("stats-exporter")
     class StatsExporter(Exporter):
         @dataclass
         class Config(Exporter.Config):
             pass

         cfg: Config

         def __call__(self) -> List[ExporterOutput]:
             mesh = self.geometry.isosurface()          # 复用训练好的几何(u8-l3)
             n_verts = mesh.v_pos.shape[0]
             n_faces = mesh.t_pos_idx.shape[0]
             bbox_min = mesh.v_pos.min(dim=0).values.tolist()
             bbox_max = mesh.v_pos.max(dim=0).values.tolist()
             payload = {
                 "n_vertices": n_verts,
                 "n_faces": n_faces,
                 "bbox_min": bbox_min,
                 "bbox_max": bbox_max,
             }
             return [
                 ExporterOutput(
                     save_name="model-stats",   # 产物文件主干名
                     save_type="json",          # 反射到 SaverMixin.save_json
                     params={"payload": payload},
                 )
             ]
     ```

   - 用 u8-l3 的导出命令切换到新扩展(**示例命令**,`<trial>` 换成某次 geometry/texture 阶段的 trial 目录):

     ```bash
     python launch.py --config <trial>/configs/parsed.yaml --export \
       resume=<trial>/ckpts/last.ckpt \
       custom_import="[my_ext.stats_exporter]" \
       system.exporter_type=stats-exporter
     ```

     注意 `custom_import` 是列表类型,命令行覆盖需带方括号;`exporter_type` 默认值就是 `mesh-exporter`([systems/base.py:L238](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L238)),这里显式换成新注册名。
   - 检查 `<trial>/save/it*-export/model-stats.json`。
3. **需要观察的现象**:启动日志里先打印 `['my_ext.stats_exporter']`(launch.py L103 的 `print(cfg.custom_import)`),随后导出流程正常走到 `on_predict_epoch_end`;若把 `save_type` 改成不存在的 `foo`,会抛 `ValueError: save_foo not supported by the SaverMixin`——这验证了 4.5.3 的反射契约。
4. **预期结果**:JSON 文件内容与网格规模一致(例如 DMTet 阶段 `n_vertices` 为数万量级);同时因为 `save_video=False`,`predict_step` 不会再渲染视频。
5. 需要可用检查点与 GPU,**待本地验证**;没有 ckpt 时,可以先写一个只 `import my_ext.stats_exporter` 然后 `print(threestudio.find("stats-exporter"))` 的三行脚本,验证注册链路本身通畅(不需要 GPU)。

#### 4.5.5 小练习与答案

**练习 1**:如果把 `StatsExporter` 直接放进 `threestudio/models/exporters/` 但不修改该包的 `__init__.py`,它能被 `find` 到吗?
**答案**:不能。注册靠 import 副作用,孙包 `__init__.py` 没 import 它,装饰器就不会执行,文件等于死代码(u3-l1 的「不在导入链上的注册不生效」)。要么在 `__init__.py` 加一行 import,要么用 `custom_import` 在运行时注入。

**练习 2**:想让新 exporter 同时导出网格与统计信息,`__call__` 应返回什么?
**答案**:返回两个 `ExporterOutput` 的列表——`save_type="obj-mtl"` 的条目(或直接在类内调用 mesh-exporter 的逻辑)加 `save_type="json"` 的统计条目。`on_predict_epoch_end` 会 for 循环逐条调用对应 `save_*`,产物都落在同一个 `it{N}-export/` 目录下。

**练习 3**:为什么 `custom_import` 的注入点在 launch.py 里必须位于 `threestudio.find(cfg.data_type)` 之前?
**答案**:`find` 查的是 `__modules__` 字典,而字典里的条目要靠 import 你的扩展模块来写入。若先 find 后 import,第一个被查找的 `data_type` 若是扩展注册名就会 `KeyError`;即便是内置名,后续 system 配置里的扩展 `*_type` 也查不到。当前代码顺序(L100 load_config → L102 custom_import → L109 find)保证了所有注册在任何查找之前完成。

## 5. 综合实践

**任务:给 DreamCraft3D 装上一个「网页可用的网格体检」扩展,并走通 Gradio 全链路。**

分四步,把本讲四个模块串起来:

1. **修复前端**(4.1):按 4.1.4 把 `model_config` 指向一份可用的 coarse 配置(或从上游 threestudio 拷回 `configs/gradio/*.yaml`),并在 dummy cli_args 中补齐 `data.image_path` 等必填项,使 `python gradio_app.py` 能渲染界面。
2. **短训练**(4.2/4.3):在网页上把 `Number of training steps` 拉到最小值,启动一次粗阶段训练;同时用两个 `watch` 终端盯住 `progress` 与 `alive`,中途关闭页签验证看门狗 10 秒收尾;再正常跑完一次,确认训练结束后**自动**出现测试视频与导出目录(fit→test→predict 三连)。
3. **扩展**(4.5):实现 4.5.4 的 `stats-exporter`,并在命令行 `--export` 下用 `custom_import` 切换成功,拿到 `model-stats.json`。
4. **串接**(4.5.3):思考题——若想让网页训练结束后的 `gr.Model3D` 面板旁边多显示一份统计,你会改哪里?参考答案:不必改前端;让 stats-exporter 与 mesh-exporter 一样产出 `*export` 目录下的文件即可被 `get_current_status` 的 glob 捞到(文本类产物需要给页面加一个 `gr.JSON` 组件并在 `run()` 的 outputs 里接收,这属于前端改动)。

没有 GPU 的环境可降级执行第 3、4 步的注册链路验证(三行脚本 `find` 探测)与 4.4 的快照观察,并对其余步骤标注「待本地验证」。

## 6. 本讲小结

- `gradio_app.py` 是**三进程松耦合架构**:Gradio 前端、`launch.py --gradio` 训练子进程、`gradio_app.py watch` 看门狗,彼此只通过试验目录下的 `progress`/`logs`/`alive` 三个文件与进程信号通信。
- 前端的一切旋钮最终都翻译成 `launch.py` 的命令行 extras 覆盖(点号语法);`name`/`tag`/`exp_root_dir`/`use_timestamp` 四个覆盖保证双方指向同一个 trial_dir;`--gradio` 在 launch.py 里引发四处变化:无色日志、FileHandler 落盘、ProgressCallback 替换 TQDM、训练后自动 predict 导出。
- `alive` 心跳由**前端**每次轮询写入,watcher 据此在界面失联 10 秒后 `SIGKILL` 训练进程,防止孤儿 GPU 任务;`ProgressCallback` 用 `seek+truncate` 原地覆写单行进度。
- `CodeSnapshotCallback`(git ls-files 双查询并集,排除 `load/`、含未跟踪文件)与 `ConfigSnapshotCallback`(parsed.yaml + raw.yaml)加上 `cmd.txt`,构成不依赖 git 历史的完整可复现闭环。
- 本仓库 `configs/gradio/` 缺失,`gradio_app.py` 直接启动会 `FileNotFoundError`——它是上游 threestudio 的继承代码,用前需补配置或改 `model_config` 表。
- 二次开发地图:九类可插拔组件(system/data/geometry/material/background/renderer/guidance/prompt_processor/exporter)全部遵循「`X_type` 注册名 + `X` 参数段 + `find` 实例化」,`custom_import` 提供零侵入接入;exporter 契约是 `ExporterOutput` 列表且 `save_type` 反射到 SaverMixin,guidance 契约是返回 dict 且 `loss_*` 键经 `lambda_*` 自动计权;callbacks 是唯一必须改 launch.py 的扩展点。

## 7. 下一步学习建议

本讲是学习手册的最后一讲,至此你已经走完了从「项目是什么」到「能改造它」的完整路径。接下来建议:

1. **动手做一个完整的小项目**:挑一个扩展点(推荐 guidance,以 [clip_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/clip_guidance.py) 为骨架)实现自己的先验,在 coarse 阶段用 `custom_import + system.guidance_type` 切换,对照 u7-l2 的 SDS 消融方法比较几何演化。
2. **重读核心算法源码**:u7 单元的 BSD 引导([stable_diffusion_bsd_guidance.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/models/guidance/stable_diffusion_bsd_guidance.py))是本仓库最有论文价值的代码,结合论文原文再读一遍 `compute_grad_vsd`/`train_lora`/`train_pretrain` 的交替闭环。
3. **对照阅读上游 threestudio 仓库**:本项目的 `gradio_app.py`、callbacks、注册机制均继承自上游;diff 两个仓库能看清 DreamCraft3D 到底「改了什么」——这是理解研究型代码工程演化的最好练习。
4. **关注社区后续工作**:如 DreamCraft3D 的后续改进与其他基于得分蒸馏的 3D 生成方法,尝试用本手册建立的分析框架(配置对比 → 调用链追踪 → 损失解剖)去快速读懂它们。
