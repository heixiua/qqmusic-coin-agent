# QQ音乐金币 Agent

用 **免费云端多模态 API** 做视觉 Agent，通过 adb 自动刷 QQ音乐「金币狂欢节」的广告任务领金币。

> ⚠️ **免责声明（先看）**
> 本项目仅供个人学习自动化与多模态 API 调用技术。
> 用脚本刷金币存在**账号被风控 / 金币清零 / 限制使用**的风险，
> **账号被封与作者无关**。代码可以拿去用，但**不建议一直挂机刷**，请理性使用、后果自负。

---

## 这个项目是干什么的？

每天手动点 QQ音乐 的金币广告太累，这个脚本帮你自动点：

```
电脑截屏手机画面 → 云端AI看图认出任务按钮 → 自动点击/等待/关闭广告 → 循环领金币
```

三个免费 AI 模型分工（一个挂了自动换下一个）：
1. **Agnes**（主）：认图 + 返回按钮坐标
2. **智谱 GLM**（备用）：同上
3. **硅基 DeepSeek-OCR**（兜底）：只认文字，判断页面状态

---

## 小白快速上手（5 步）

### 第 1 步：装 Python
去 https://www.python.org/downloads/ 下载安装，安装时**勾选 "Add Python to PATH"**。

### 第 2 步：装依赖
打开命令行（Win+R 输入 `cmd` 回车），执行：
```
pip install openai pillow python-dotenv
```

### 第 3 步：配置密钥（拿 3 个免费 API Key）
1. 复制项目里的 **`.env.example`** 文件，改名为 **`.env`**
   （如果看不到 `.env.example`，在文件夹里开「显示隐藏文件」）
2. 去下面 3 个网站免费注册，各自拿一串 Key：
   - **Agnes**（主模型）：https://apihub.agnes-ai.com
   - **智谱 GLM**（备用）：https://open.bigmodel.cn
   - **硅基流动**（兜底）：https://cloud.siliconflow.cn
3. 把 3 个 Key 分别填到 `.env` 里对应位置（`=` 号后面），保存

> 💡 只填 `AGNES_API_KEY` 一个也能跑，其他是备用。

### 第 4 步：手机连电脑
1. 手机开启「开发者选项 → USB 调试」（不会就搜索"安卓 USB调试怎么开"）
2. USB 数据线连电脑，手机弹窗选「允许调试」
3. 下载 adb 工具：https://developer.android.com/tools/releases/platform-tools
   解压后把里面的 `platform-tools` 文件夹放到本项目目录下
4. 命令行验证（看到设备编号即成功）：
```
platform-tools\platform-tools\adb.exe devices
```

### 第 5 步：运行
1. 手机打开 QQ音乐，进入「金币狂欢节 / 赚金币」页面（就是你平时领金币那个页面）
2. 命令行执行：
```
python qqmusic_agent.py 100
```
3. 脚本开始自动刷，日志实时打印在窗口里。100 是轮数，可以自己改。

> ⏹ 想停止：直接关掉命令行窗口即可，不影响手机。

---

## 给懂行的人（环境变量 / .env）

项目用 `.env` 文件读密钥（已提交 `.env.example` 模板）：
```
AGNES_API_KEY=...           # 主视觉模型（免费）
ZHIPUAI_API_KEY=...         # 备用视觉模型（免费）
SILICONFLOW_API_KEY=...     # OCR 兜底（免费）
SILICONFLOW_OCR_MODEL=deepseek-ai/DeepSeek-OCR
AGNES_MODEL=agnes-3.0-flash # 默认即可
```
- `.env` 已被 `.gitignore` 排除，**不会**上传到 GitHub
- 运行目录需包含：`qqmusic_agent.py` + `cap_screen.py` + `.env` + `platform-tools/`

## 文件说明

| 文件 | 作用 |
|---|---|
| `qqmusic_agent.py` | 主脚本：状态机 + AI 调用 + adb 控制（唯一需要运行的文件） |
| `cap_screen.py` | adb 截屏辅助（主脚本自动调用，不用管） |
| `.env.example` | 密钥配置模板（复制改名为 `.env` 后填写） |
| `.gitignore` | 排除 `.env`（密钥）和本地工具的规则 |

## 工作原理（给好奇的人）

- **AI 负责"看和选"**：识别页面任务、定位按钮坐标
- **代码负责"流程和兜底"**：广告等待时长（16秒）、关闭路径、卡住自动恢复
- 金币页用 uiautomator 直接读界面文本（硬编码优先，省 API 调用），读不到才调 AI
- 广告页有状态机：进广告 → 点跳转按钮 → 等 16 秒 → 退出 → 关闭 → 回金币页
- 三层兜底：误跳第三方强制拉回 / 连续无动作强制返回 / 连滑到底强制回顶

## 常见问题

- **没反应 / 日志报 adb 找不到**：platform-tools 没放对位置，检查第 4 步
- **AI 密钥报错**：`.env` 里 Key 填错或没填，检查第 3 步
- **金币不涨了**：当天任务刷完了，或账号被限流——停下来歇一歇
- **卡住不动超过 1 分钟**：多半是新广告形态，把日志截图发到 Issues 里
