// Native single-page presentation: no browser runtime needed by the installer.
var
  Surface, Hero, PrimaryAction: TPanel;
  Brand, Headline, Intro, Feature, Stage, Footnote, DirectoryLabel: TLabel;
  Backdrop: TBitmapImage;
  CloseAction, DragSurface: TLabel;
  DirectoryCard, DirectoryButton: TPanel;
  DirectoryPath: TLabel;
  Progress, ProgressFill: TPanel;
  LaunchOption: TLabel;
  LaunchChecked: Boolean;
  WaveFrames: array[0..79] of TBitmap;
  WaveTimer: UINT_PTR;
  WaveTick, ProgressTick: Integer;
  RetryButton: TNewButton;
  CurrentPage: Integer;
  Ready, Configuring: Boolean;

function SetTimer(hWnd: HWND; nIDEvent: UINT_PTR; uElapse: UINT; lpTimerFunc: LongWord): UINT_PTR;
  external 'SetTimer@user32.dll stdcall';
function KillTimer(hWnd: HWND; uIDEvent: UINT_PTR): Boolean;
  external 'KillTimer@user32.dll stdcall';
function GetWindowLong(hWnd: HWND; Index: Integer): Longint;
  external 'GetWindowLongW@user32.dll stdcall';
function SetWindowLong(hWnd: HWND; Index: Integer; Value: Longint): Longint;
  external 'SetWindowLongW@user32.dll stdcall';

procedure WaveFrame(A: HWND; B: UINT; C: UINT_PTR; D: DWORD);
begin
  WaveTick := (WaveTick + 1) mod 80;
  Backdrop.Bitmap.Assign(WaveFrames[WaveTick]);
  if Configuring then begin
    ProgressFill.Width := ScaleX(100);
    ProgressFill.Left := Round((Progress.Width + ProgressFill.Width) * ProgressTick / 79.0) - ProgressFill.Width;
    ProgressTick := (ProgressTick + 1) mod 80;
  end;
end;

procedure ToggleLaunch(Sender: TObject);
var I: Integer;
begin
  if not Ready then Exit;
  LaunchChecked := not LaunchChecked;
  if LaunchChecked then LaunchOption.Caption := '☑  立即启动 {#AppName}'
  else LaunchOption.Caption := '☐  立即启动 {#AppName}';
  for I := 0 to WizardForm.RunList.Items.Count - 1 do
    WizardForm.RunList.Checked[I] := LaunchChecked;
end;

function ReleaseCapture: Boolean;
  external 'ReleaseCapture@user32.dll stdcall';
function WindowMessage(hWnd: HWND; Msg: Cardinal; WParam, LParam: Longint): Longint;
  external 'SendMessageW@user32.dll stdcall';

function IsHomePage(PageID: Integer): Boolean;
begin
  Result := (PageID = wpSelectDir) or (PageID = wpReady) or (PageID = wpWelcome);
end;

procedure DragWindow(Sender: TObject; Button: TMouseButton; Shift: TShiftState; X, Y: Integer);
begin
  if Button = mbLeft then begin
    ReleaseCapture;
    WindowMessage(WizardForm.Handle, $00A1, 2, 0);
  end;
end;

procedure CloseClick(Sender: TObject);
var I: Integer;
begin
  if (CurrentPage = wpPreparing) or (CurrentPage = wpInstalling) or Configuring then Exit;
  if CurrentPage = wpFinished then begin
    for I := 0 to WizardForm.RunList.Items.Count - 1 do WizardForm.RunList.Checked[I] := False;
    WizardForm.NextButton.OnClick(WizardForm.NextButton);
  end else begin
    // Inno only accepts Close when its native CancelButton.CanFocus is true.
    WizardForm.CancelButton.Visible := True;
    WizardForm.CancelButton.Enabled := True;
    WindowMessage(WizardForm.CancelButton.Handle, $00F5, 0, 0);
  end;
end;

function InstallReady: Boolean;
begin
  Result := Ready;
end;

procedure PrimaryClick(Sender: TObject);
var PreviousPage, Attempts: Integer;
begin
  if not WizardForm.NextButton.Enabled then Exit;
  if CurrentPage = wpFinished then begin
    WizardForm.NextButton.OnClick(WizardForm.NextButton);
    Exit;
  end;
  Attempts := 0;
  // Advance hidden welcome/ready pages in one click, retaining directory validation.
  while IsHomePage(CurrentPage) and WizardForm.NextButton.Enabled and (Attempts < 3) do begin
    PreviousPage := CurrentPage;
    Inc(Attempts);
    WizardForm.NextButton.OnClick(WizardForm.NextButton);
    if CurrentPage = PreviousPage then Break;
  end;
end;

function LabelAt(Parent: TWinControl; X, Y, W, H, Size: Integer; Text: String; Color: TColor): TLabel;
begin
  Result := TLabel.Create(WizardForm);
  Result.Parent := Parent;
  Result.SetBounds(ScaleX(X), ScaleY(Y), ScaleX(W), ScaleY(H));
  Result.AutoSize := False;
  Result.WordWrap := True;
  Result.Transparent := True;
  Result.Alignment := taCenter;
  Result.Font.Name := 'Microsoft YaHei UI';
  Result.Font.Size := Size;
  Result.Font.Color := Color;
  Result.Caption := Text;
  Result.OnMouseDown := @DragWindow;
end;

procedure SelectDirectory(Sender: TObject);
var Selected: String;
begin
  Selected := WizardForm.DirEdit.Text;
  if BrowseForFolder('选择安装目录', Selected, True) then begin
    WizardForm.DirEdit.Text := Selected;
    DirectoryPath.Caption := MinimizePathName(Selected, DirectoryPath.Font, ScaleX(385));
    DirectoryPath.Hint := Selected;
  end;
end;

procedure ShowFeature(Phase: Integer);
begin
  case Phase of
    0: begin
#if MyRole == "client"
      Headline.Caption := '同态加密，让 AI 在密文上计算。';
      Intro.Caption := '结构化数值保持加密，大模型不接触数值明文。';
      Feature.Caption := '自然语言分析  /  本机加密  /  授权解密';
#else
      Headline.Caption := '同态加密，让企业数据安全协作。';
      Intro.Caption := '统一管理数据权限与密文运算，大模型不接触数值明文。';
      Feature.Caption := '多用户管理  /  数据权限  /  使用审计';
#endif
    end;
    1: begin
      Headline.Caption := '安全边界，清晰可见。';
      Intro.Caption := '结构化数值先在本机加密，涉及文档正文和解密的步骤需明确授权。';
      Feature.Caption := '权限控制  /  过程可见  /  操作可追溯';
    end;
    2: begin
      Headline.Caption := '运行环境，为你准备。';
      Intro.Caption := '自动安装随包依赖与运行组件，无需手动配置 Python 环境。';
      Feature.Caption := '离线依赖  /  自动配置  /  启动检查';
    end;
    3: begin
      Headline.Caption := '准备就绪，开始使用。';
      Intro.Caption := '程序与运行环境已安装，并已通过本机启动检查。';
      Feature.Caption := 'Clawworker  /  {#AppName}  /  v{#AppVersion}';
    end;
  end;
end;

procedure ConfigurationOutput(const S: String; const Error, FirstLine: Boolean);
begin
  Log(S);
  SaveStringToFile(ExpandConstant('{app}\packaging\windows\install-{#RoleArg}.log'), S + #13#10, True);
  if Pos('CW_STAGE:PYTHON', S) > 0 then Stage.Caption := '正在准备 Python 运行环境…';
  if Pos('CW_STAGE:DEPENDENCIES', S) > 0 then Stage.Caption := '正在安装离线依赖…';
  if Pos('CW_STAGE:HE', S) > 0 then Stage.Caption := '正在安装同态计算组件…';
  if Pos('CW_STAGE:SYSTEM', S) > 0 then Stage.Caption := '正在配置系统组件…';
  if Pos('CW_STAGE:VERIFY', S) > 0 then Stage.Caption := '正在进行真实启动检查…';
end;

procedure ConfigureRuntime;
var Code: Integer; Executed: Boolean;
begin
  if Configuring then Exit;
  Configuring := True;
  Ready := False;
  RetryButton.Visible := False;
  WizardForm.NextButton.Enabled := False;
  WizardForm.CancelButton.Enabled := False;
  CloseAction.Enabled := False;
  Progress.Visible := True;
  ProgressFill.Width := ScaleX(100);
  ProgressTick := 0;
  ProgressFill.Left := -ProgressFill.Width;
  Stage.Caption := '正在配置运行环境…';
  Footnote.Caption := '依赖配置耗时因电脑性能而异，请保持安装窗口开启。';
  ShowFeature(2);
  SaveStringToFile(ExpandConstant('{app}\packaging\windows\install-{#RoleArg}.log'), '', False);
  try
    Executed := ExecAndLogOutput(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-ExecutionPolicy Bypass -NoProfile -File "' + ExpandConstant('{app}\packaging\windows\install.ps1') + '" -Role {#RoleArg} -NoShortcut',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code, @ConfigurationOutput);
    Ready := Executed and (Code = 0);
  except
    Log(GetExceptionMessage);
    Ready := False;
  end;
  Configuring := False;
  ProgressFill.Left := 0;
  if Ready then begin
    ProgressFill.Width := Progress.Width;
    Stage.Caption := '安装与启动检查已完成';
    Footnote.Caption := '';
    ShowFeature(3);
  end else begin
    Progress.Visible := False;
    Headline.Caption := '还差一步，完成配置。';
    Intro.Caption := '程序文件已复制，但运行环境配置未完成。请查看日志后重试。';
    Stage.Caption := '配置未完成，不会自动启动程序';
    Footnote.Caption := '日志：' + ExpandConstant('{app}\packaging\windows\install-{#RoleArg}.log');
    RetryButton.Visible := not WizardSilent;
  end;
  WizardForm.NextButton.Enabled := True;
  WizardForm.CancelButton.Enabled := CurrentPage = wpFinished;
  CloseAction.Enabled := CurrentPage = wpFinished;
end;

procedure RetryClick(Sender: TObject);
begin
  ConfigureRuntime;
  WizardForm.RunList.Visible := False;
  LaunchOption.Visible := Ready;
  if Ready then WizardForm.NextButton.Caption := '完成' else WizardForm.NextButton.Caption := '关闭';
  PrimaryAction.Caption := WizardForm.NextButton.Caption;
end;

procedure InitializeWizard;
var I: Integer;
begin
  Ready := False;
  // Keep role-specific upgrades in place; migrate only legacy shared roots.
  if CompareText(ExtractFileName(RemoveBackslash(WizardForm.DirEdit.Text)), 'Clawworker') = 0 then
    WizardForm.DirEdit.Text := AddBackslash(WizardForm.DirEdit.Text) + '{#RoleArg}';
  WizardForm.BorderStyle := bsNone;
  WizardForm.ClientWidth := ScaleX(880);
  WizardForm.ClientHeight := ScaleY(600);
  WizardForm.Caption := '{#AppName} 安装';
  WizardForm.Color := clWhite;
  WizardForm.MainPanel.Visible := False;
  WizardForm.InnerNotebook.Visible := False;
  WizardForm.Bevel.Visible := False;
  Surface := TPanel.Create(WizardForm);
  Surface.Parent := WizardForm;
  Surface.SetBounds(0, 0, ScaleX(880), ScaleY(600));
  Surface.BevelOuter := bvNone;
  Surface.ParentBackground := False;
  Surface.Color := clWhite;
  Hero := TPanel.Create(WizardForm);
  Hero.Parent := Surface;
  Hero.SetBounds(0, 0, ScaleX(880), ScaleY(600));
  Hero.BevelOuter := bvNone;
  Hero.ParentBackground := False;
  Hero.Color := clWhite;
  SetWindowLong(Hero.Handle, -20, GetWindowLong(Hero.Handle, -20) or $02000000);
  ExtractTemporaryFile('installer-backdrop.bmp');
  Backdrop := TBitmapImage.Create(WizardForm);
  Backdrop.Parent := Hero;
  Backdrop.SetBounds(0, 0, ScaleX(880), ScaleY(600));
  Backdrop.Stretch := True;
  Backdrop.Bitmap.LoadFromFile(ExpandConstant('{tmp}\installer-backdrop.bmp'));
  DragSurface := LabelAt(Hero, 0, 0, 880, 600, 10, '', clWhite);
  Brand := LabelAt(Hero, 100, 213, 680, 48, 30, 'ClawWorker', $003F2310);
  Brand.Font.Style := [fsBold];
  Headline := LabelAt(Hero, 100, 276, 680, 34, 17, '', $003F2310);
  Headline.Font.Style := [fsBold];
  Intro := LabelAt(Hero, 125, 318, 630, 42, 10, '', $008B7565);
  Feature := LabelAt(Hero, 100, 375, 680, 26, 10, '', $00EB6324);
  Feature.Visible := False;
  LabelAt(Hero, 100, 56, 680, 22, 10, '{#AppName}  ·  v{#AppVersion}', $008B7565);
  DirectoryLabel := LabelAt(Hero, 190, 425, 500, 22, 10, '安装位置', $008B7565);
  WizardForm.DirEdit.Parent := Hero;
  WizardForm.DirEdit.SetBounds(ScaleX(190), ScaleY(452), ScaleX(398), ScaleY(28));
  WizardForm.DirBrowseButton.Parent := Hero;
  WizardForm.DirBrowseButton.SetBounds(ScaleX(598), ScaleY(452), ScaleX(92), ScaleY(28));
  WizardForm.DirBrowseButton.Caption := '更改目录';
  DirectoryCard := TPanel.Create(WizardForm);
  DirectoryCard.Parent := Hero;
  DirectoryCard.SetBounds(ScaleX(160), ScaleY(424), ScaleX(560), ScaleY(46));
  DirectoryCard.BevelOuter := bvNone;
  DirectoryCard.ParentBackground := False;
  DirectoryCard.Color := $00FCF7F2;
  DirectoryPath := LabelAt(DirectoryCard, 16, 14, 385, 22, 10, '', $008B7565);
  DirectoryPath.Alignment := taLeftJustify;
  DirectoryPath.ShowHint := True;
  DirectoryPath.Cursor := crHand;
  DirectoryPath.OnClick := @SelectDirectory;
  DirectoryPath.OnMouseDown := nil;
  DirectoryButton := TPanel.Create(WizardForm);
  DirectoryButton.Parent := DirectoryCard;
  DirectoryButton.SetBounds(ScaleX(415), ScaleY(0), ScaleX(145), ScaleY(46));
  DirectoryButton.BevelOuter := bvNone;
  DirectoryButton.ParentBackground := False;
  DirectoryButton.Color := $00FFF0E5;
  DirectoryButton.Font.Name := 'Microsoft YaHei UI';
  DirectoryButton.Font.Size := 10;
  DirectoryButton.Font.Color := $00EB6324;
  DirectoryButton.Caption := '选择安装目录';
  DirectoryButton.Cursor := crHand;
  DirectoryButton.OnClick := @SelectDirectory;
  Stage := LabelAt(Hero, 190, 452, 500, 28, 11, '', $003D2114);
  Stage.Visible := False;
  Progress := TPanel.Create(WizardForm);
  Progress.Parent := Hero;
  Progress.SetBounds(ScaleX(240), ScaleY(502), ScaleX(400), ScaleY(6));
  Progress.BevelOuter := bvNone;
  Progress.ParentBackground := False;
  Progress.Color := $00F2DFCB;
  ProgressFill := TPanel.Create(WizardForm);
  ProgressFill.Parent := Progress;
  ProgressFill.BevelOuter := bvNone;
  ProgressFill.ParentBackground := False;
  ProgressFill.Color := $00EB6324;
  ProgressFill.SetBounds(0, 0, 0, ScaleY(6));
  Progress.Visible := False;
  Footnote := LabelAt(Hero, 80, 566, 720, 30, 9, '', $00857564);
  RetryButton := TNewButton.Create(WizardForm);
  RetryButton.Parent := Hero;
  RetryButton.SetBounds(ScaleX(240), ScaleY(500), ScaleX(150), ScaleY(36));
  RetryButton.Caption := '重试配置';
  RetryButton.OnClick := @RetryClick;
  RetryButton.Visible := False;
  WizardForm.BackButton.Visible := False;
  WizardForm.NextButton.Parent := Hero;
  WizardForm.NextButton.SetBounds(ScaleX(340), ScaleY(500), ScaleX(200), ScaleY(38));
  PrimaryAction := TPanel.Create(WizardForm);
  PrimaryAction.Parent := Hero;
  PrimaryAction.SetBounds(ScaleX(340), ScaleY(500), ScaleX(200), ScaleY(38));
  PrimaryAction.BevelOuter := bvNone;
  PrimaryAction.ParentBackground := False;
  PrimaryAction.Color := $00EB6324;
  PrimaryAction.Font.Name := 'Microsoft YaHei UI';
  PrimaryAction.Font.Size := 11;
  PrimaryAction.Font.Color := clWhite;
  PrimaryAction.Caption := '立即安装';
  PrimaryAction.Cursor := crHand;
  PrimaryAction.OnClick := @PrimaryClick;
  WizardForm.CancelButton.Parent := Hero;
  WizardForm.CancelButton.SetBounds(-100, -100, 1, 1);
  WizardForm.CancelButton.Caption := '×';
  WizardForm.CancelButton.Visible := True;
  WizardForm.CancelButton.TabStop := False;
  CloseAction := LabelAt(Hero, 836, 12, 30, 28, 18, '×', $008B7565);
  CloseAction.Cursor := crHand;
  CloseAction.OnClick := @CloseClick;
  CloseAction.OnMouseDown := nil;
  LaunchOption := LabelAt(Hero, 280, 550, 320, 26, 10, '☑  立即启动 {#AppName}', $008B7565);
  LaunchOption.OnMouseDown := nil;
  LaunchOption.OnClick := @ToggleLaunch;
  LaunchOption.Cursor := crHand;
  LaunchOption.Visible := False;
  LaunchChecked := True;
  for I := 0 to 79 do begin
    ExtractTemporaryFile('wave-' + IntToStr(I) + '.bmp');
    WaveFrames[I] := TBitmap.Create;
    WaveFrames[I].LoadFromFile(ExpandConstant('{tmp}\\wave-') + IntToStr(I) + '.bmp');
  end;
  ShowFeature(0);
  WaveTimer := SetTimer(0, 0, 50, CreateCallback(@WaveFrame));
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = wpWelcome) or (PageID = wpSelectProgramGroup) or (PageID = wpSelectTasks) or (PageID = wpReady);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  CurrentPage := CurPageID;
  // Inno applies its automatic page sizing after InitializeWizard.
  WizardForm.ClientWidth := ScaleX(880);
  WizardForm.ClientHeight := ScaleY(600);
  Log('Presentation page: ' + IntToStr(CurPageID));
  WizardForm.CancelButton.Caption := '×';
  WizardForm.CancelButton.Enabled := (CurPageID <> wpPreparing) and
    (CurPageID <> wpInstalling) and not Configuring;
  CloseAction.Enabled := WizardForm.CancelButton.Enabled;
  WizardForm.CancelButton.Visible := True;
  WizardForm.BackButton.Visible := False;
  WizardForm.DirEdit.Visible := False;
  WizardForm.DirBrowseButton.Visible := False;
  DirectoryLabel.Visible := False;
  DirectoryCard.Visible := IsHomePage(CurPageID);
  DirectoryPath.Caption := MinimizePathName(WizardForm.DirEdit.Text, DirectoryPath.Font, ScaleX(385));
  DirectoryPath.Hint := WizardForm.DirEdit.Text;
  Stage.Visible := not IsHomePage(CurPageID);
  WizardForm.NextButton.Visible := False;
  PrimaryAction.Visible := IsHomePage(CurPageID) or (CurPageID = wpFinished);
  if CurPageID = wpSelectDir then WizardForm.NextButton.Caption := '立即安装';
  if CurPageID = wpInstalling then begin
    ShowFeature(1);
    Progress.Visible := True;
    Footnote.Caption := '正在安装程序文件，随后配置依赖并检查启动状态。';
  end;
  if CurPageID = wpFinished then begin
    Progress.Visible := False;
    WizardForm.RunList.Parent := Hero;
    WizardForm.RunList.SetBounds(ScaleX(340), ScaleY(535), ScaleX(360), ScaleY(24));
    WizardForm.RunList.Visible := False;
    LaunchOption.Visible := Ready and (WizardForm.RunList.Items.Count > 0);
    if Ready then WizardForm.NextButton.Caption := '完成' else WizardForm.NextButton.Caption := '关闭';
    PrimaryAction.Caption := WizardForm.NextButton.Caption;
    if not Ready then PrimaryAction.Left := ScaleX(490);
  end;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  // Covers the close button, Escape and Alt+F4 without an exit dialog.
  Confirm := False;
  Cancel := (CurPageID <> wpPreparing) and (CurPageID <> wpInstalling) and
    not Configuring;
end;

procedure CurInstallProgressChanged(CurProgress, MaxProgress: Integer);
var P: Integer;
begin
  if MaxProgress > 0 then begin
    P := Round(CurProgress * 100.0 / MaxProgress);
    ProgressFill.Left := 0;
    ProgressFill.Width := Round(Progress.Width * P / 100.0);
    Stage.Caption := '正在安装程序文件 · ' + IntToStr(P) + '%';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then ConfigureRuntime;
end;

function GetCustomSetupExitCode: Integer;
begin
  if Ready then Result := 0 else Result := 1;
end;

procedure DeinitializeSetup;
var I: Integer;
begin
  if WaveTimer <> 0 then KillTimer(0, WaveTimer);
  for I := 0 to 79 do if Assigned(WaveFrames[I]) then WaveFrames[I].Free;
end;
