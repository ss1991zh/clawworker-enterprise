from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging/windows'


def test_installer_gates_launch_on_dependency_success():
    ui = (PACKAGING / 'installer-modern.iss').read_text(encoding='utf-8')
    setup = (PACKAGING / 'clawworker-setup.iss').read_text(encoding='utf-8')
    assert 'Ready := Executed and (Code = 0)' in ui
    assert 'Check: InstallReady' in setup
    assert 'function GetCustomSetupExitCode' in ui
    assert "if Ready then Result := 0 else Result := 1" in ui
    assert 'ExecAndLogOutput' in ui
    assert 'procedure RetryClick' in ui


def test_progress_uses_real_events_and_not_fake_completion():
    ui = (PACKAGING / 'installer-modern.iss').read_text(encoding='utf-8')
    script = (PACKAGING / 'install.ps1').read_text(encoding='utf-8-sig')
    assert 'CurInstallProgressChanged' in ui
    assert 'ProgressFill.Width := Round(Progress.Width * P / 100.0)' in ui
    assert 'ProgressFill.Color := $00EB6324' in ui
    assert 'TNewProgressBar.Create' not in ui
    assert 'procedure Animate' not in ui
    assert 'installer-backdrop.bmp' in ui
    assert 'Hero.Color := clWhite' in ui
    for stage in ('PYTHON', 'DEPENDENCIES', 'HE', 'SYSTEM', 'VERIFY'):
        assert f'CW_STAGE:{stage}' in script
        assert f'CW_STAGE:{stage}' in ui


def test_packaging_keeps_existing_data_and_role_separation():
    setup = (PACKAGING / 'clawworker-setup.iss').read_text(encoding='utf-8')
    assert 'UsePreviousAppDir=yes' in setup
    assert 'Clawworker\\{#RoleArg}' in setup
    assert 'SetupLogging=yes' in setup
    assert 'DisableDirPage=no' in setup
    assert 'Source: "..\\..\\docs\\*"' not in setup
    assert '*user_authorization*' in setup


def test_presentation_is_centered_and_replaces_actions_with_progress():
    ui = (PACKAGING / 'installer-modern.iss').read_text(encoding='utf-8')
    assert 'WizardForm.BorderStyle := bsNone' in ui
    assert 'Result.Alignment := taCenter' in ui
    assert 'Result.Transparent := True' in ui
    assert 'PrimaryAction.OnClick := @PrimaryClick' in ui
    assert 'PrimaryAction.Visible := IsHomePage(CurPageID) or (CurPageID = wpFinished)' in ui
    assert 'DirectoryCard.Visible := IsHomePage(CurPageID)' in ui
    assert "BrowseForFolder('选择安装目录', Selected, True)" in ui
    assert 'CloseAction := LabelAt' in ui
    assert 'WizardForm.CancelButton.TabStop := False' in ui
    assert 'AnimationTimer' not in ui
    assert 'WizardForm.NextButton.OnClick(WizardForm.NextButton)' in ui


def test_close_is_immediate_only_outside_installation():
    ui = (PACKAGING / 'installer-modern.iss').read_text(encoding='utf-8')
    assert 'procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean)' in ui
    assert 'Confirm := False;' in ui
    assert 'Cancel := (CurPageID <> wpPreparing) and (CurPageID <> wpInstalling) and' in ui
    assert 'WizardForm.CancelButton.Enabled := (CurPageID <> wpPreparing) and' in ui
    assert 'WizardForm.CancelButton.Enabled := CurrentPage = wpFinished;' in ui
    assert 'WindowMessage(WizardForm.CancelButton.Handle, $00F5, 0, 0)' in ui
    assert 'WizardForm.CancelButton.SetBounds(-100, -100, 1, 1)' in ui
    assert 'WizardForm.CancelButton.Visible := False' not in ui
    assert 'WizardForm.Close;' not in ui


def test_gradient_background_and_dragging_replace_circles():
    ui = (PACKAGING / 'installer-modern.iss').read_text(encoding='utf-8')
    art = (PACKAGING / 'render_installer_backdrop.py').read_text(encoding='utf-8')
    assert 'PulseCircles' not in ui
    assert 'installer-circle' not in art
    assert 'diagonal*2*np.pi-phase' in art
    assert 'KillTimer(0, WaveTimer)' in ui
    assert 'WindowMessage(WizardForm.Handle, $00A1, 2, 0)' in ui
    assert 'DragSurface := LabelAt(Hero, 0, 0, 880, 600' in ui
    assert 'CloseAction.OnMouseDown := nil' in ui
    assert 'DirectoryPath.OnMouseDown := nil' in ui


def test_launch_option_is_transparent_and_controls_real_launch():
    ui = (PACKAGING / 'installer-modern.iss').read_text(encoding='utf-8')
    assert '首次使用请连接管理端' not in ui
    assert 'WizardForm.RunList.Visible := Ready' not in ui
    assert 'LaunchOption.OnClick := @ToggleLaunch' in ui
    assert 'WizardForm.RunList.Checked[I] := LaunchChecked' in ui
    assert 'LaunchOption := LabelAt' in ui
    assert "'☑  立即启动 {#AppName}', $008B7565)" in ui


def test_four_second_wave_and_one_way_configuration_progress():
    ui = (PACKAGING / 'installer-modern.iss').read_text(encoding='utf-8')
    assert 'SetTimer(0, 0, 50, CreateCallback(@WaveFrame))' in ui
    assert 'WaveTick := (WaveTick + 1) mod 80' in ui
    assert 'ProgressTick := (ProgressTick + 1) mod 80' in ui
    assert 'ProgressTick / 79.0) - ProgressFill.Width' in ui
    assert 'Sin(WaveTick' not in ui
