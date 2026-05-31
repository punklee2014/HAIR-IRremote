/**
 * Dialog for adding a new IR device.
 *
 * Collects name, device type, emitter selection, and — for AC devices —
 * the control mode (learned vs protocol) with optional protocol selection.
 */
import { LitElement, html, css } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import "./ir-emitter-picker.js";
import type { HairApi } from "./api.js";
import type {
    AcControlMode,
    CaptureProviderInfo,
    DeviceTypeId,
    IRDevice,
} from "./types.js";

const DEVICE_TYPES: { value: DeviceTypeId; label: string }[] = [
    { value: "media_player", label: "Media Player" },
    { value: "ac", label: "Air Conditioner" },
    { value: "fan", label: "Fan" },
    { value: "light", label: "Light" },
    { value: "switch", label: "Switch" },
    { value: "screen", label: "Screen / Shade" },
    { value: "other", label: "Other" },
];

@customElement("ir-add-device-dialog")
export class IrAddDeviceDialog extends LitElement {
    @property({ attribute: false }) public api!: HairApi;
    @property({ attribute: false }) public hass: any;

    @state() private _name = "";
    @state() private _deviceType: DeviceTypeId = "media_player";
    @state() private _emitterIds: string[] = [];
    @state() private _captureProviders: CaptureProviderInfo[] = [];
    @state() private _busy = false;
    @state() private _error: string | null = null;

    // Protocol AC fields
    @state() private _acControlMode: AcControlMode = "learned";
    @state() private _irProtocol: string | null = null;
    @state() private _protocols: string[] = [];

    connectedCallback(): void {
        super.connectedCallback();
        void this._loadCaptureProviders();
    }

    private async _loadCaptureProviders() {
        try {
            this._captureProviders = await this.api.listCaptureProviders();
        } catch {
            // Non-fatal.
        }
    }

    private async _loadProtocols() {
        if (this._protocols.length > 0) return;
        try {
            const result = await this.api.listProtocols();
            this._protocols = result.protocols;
        } catch {
            this._protocols = [];
        }
    }

    private _close() {
        this.dispatchEvent(
            new CustomEvent("closed", { bubbles: true, composed: true }),
        );
    }

    private async _create() {
        if (!this._name.trim()) {
            this._error = "Name is required.";
            return;
        }
        if (this._emitterIds.length === 0) {
            this._error = "Pick at least one IR emitter.";
            return;
        }

        this._busy = true;
        this._error = null;
        try {
            const provider = this._captureProviders[0] ?? null;
            const created: IRDevice = await this.api.createDevice({
                name: this._name.trim(),
                device_type: this._deviceType,
                emitter_entity_ids: this._emitterIds,
                capture_device_id: provider?.device_id ?? null,
                capture_provider_type: provider?.type ?? "esphome",
                ac_control_mode: this._deviceType === "ac" ? this._acControlMode : "learned",
                ir_protocol: this._deviceType === "ac" && this._acControlMode === "protocol"
                    ? this._irProtocol : null,
            });
            this.dispatchEvent(
                new CustomEvent("device-created", {
                    detail: created,
                    bubbles: true,
                    composed: true,
                }),
            );
        } catch (err) {
            this._error = (err as Error).message;
        } finally {
            this._busy = false;
        }
    }

    private _onDeviceTypeChange(type: DeviceTypeId) {
        this._deviceType = type;
        if (type === "ac") {
            void this._loadProtocols();
        }
    }

    render() {
        const isAc = this._deviceType === "ac";
        return html`
            <ha-dialog
                open
                heading="Add Device"
                scrimClickAction=""
                @closed=${this._close}
            >
                ${this._error
                    ? html`<ha-alert alert-type="error">${this._error}</ha-alert>`
                    : ""}

                <div class="field">
                    <label>Name</label>
                    <input
                        type="text"
                        .value=${this._name}
                        placeholder="e.g. Living Room AC"
                        required
                        autofocus
                        @input=${(e: Event) =>
                            (this._name = (e.target as HTMLInputElement).value)}
                    />
                </div>

                <div class="field">
                    <label>Device type</label>
                    <select
                        .value=${this._deviceType}
                        @change=${(e: Event) =>
                            this._onDeviceTypeChange((e.target as HTMLSelectElement)
                                .value as DeviceTypeId)}
                    >
                        ${DEVICE_TYPES.map(
                            (t) => html`
                                <option
                                    value=${t.value}
                                    ?selected=${this._deviceType === t.value}
                                >
                                    ${t.label}
                                </option>
                            `,
                        )}
                    </select>
                </div>

                ${isAc
                    ? html`
                        <div class="field">
                            <label>Control mode</label>
                            <div class="radio-group">
                                <label class="radio-label">
                                    <input
                                        type="radio"
                                        name="ac_control_mode"
                                        value="learned"
                                        ?checked=${this._acControlMode === "learned"}
                                        @change=${() => (this._acControlMode = "learned")}
                                    />
                                    Learned — capture each temp/mode key via Sniffer
                                </label>
                                <label class="radio-label">
                                    <input
                                        type="radio"
                                        name="ac_control_mode"
                                        value="protocol"
                                        ?checked=${this._acControlMode === "protocol"}
                                        @change=${() => (this._acControlMode = "protocol")}
                                    />
                                    Protocol (IRremoteESP8266) — encode commands automatically
                                </label>
                            </div>
                        </div>

                        ${this._acControlMode === "protocol"
                            ? html`
                                <div class="field">
                                    <label>IR Protocol</label>
                                    <select
                                        .value=${this._irProtocol || ""}
                                        @change=${(e: Event) =>
                                            (this._irProtocol = (e.target as HTMLSelectElement).value || null)}
                                    >
                                        <option value="">-- Select protocol --</option>
                                        ${this._protocols.map(
                                            (p) => html`
                                                <option
                                                    value=${p}
                                                    ?selected=${this._irProtocol === p}
                                                >
                                                    ${p}
                                                </option>
                                            `,
                                        )}
                                    </select>
                                </div>
                            `
                            : ""}
                    `
                    : ""}

                <ir-emitter-picker
                    .hass=${this.hass}
                    .value=${this._emitterIds}
                    ?disabled=${this._busy}
                    @emitters-changed=${(e: CustomEvent) =>
                        (this._emitterIds = e.detail.value)}
                ></ir-emitter-picker>

                <div class="dialog-actions">
                    <button
                        class="action-btn cancel-btn"
                        @click=${this._close}
                        ?disabled=${this._busy}
                    >
                        Cancel
                    </button>
                    <button
                        class="action-btn create-btn"
                        @click=${this._create}
                        ?disabled=${this._busy}
                    >
                        ${this._busy ? "Creating..." : "Create"}
                    </button>
                </div>
            </ha-dialog>
        `;
    }

    static styles = css`
        .field {
            display: block;
            margin: 12px 0;
            width: 100%;
        }
        .field label {
            display: block;
            font-size: 0.85rem;
            color: var(--secondary-text-color);
            margin-bottom: 6px;
        }
        .radio-group {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .radio-label {
            font-size: 0.9rem;
            color: var(--primary-text-color);
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .radio-label input[type="radio"] {
            margin: 0;
            accent-color: var(--primary-color);
        }
        input[type="text"],
        select {
            width: 100%;
            padding: 8px;
            border-radius: 4px;
            border: 1px solid var(--divider-color);
            background: var(--card-background-color);
            color: var(--primary-text-color);
            font-size: 0.95rem;
            font-family: inherit;
            box-sizing: border-box;
        }
        input[type="text"]:focus,
        select:focus {
            outline: none;
            border-color: var(--primary-color);
        }
        ha-alert {
            display: block;
            margin: 8px 0;
        }
        .dialog-actions {
            display: flex;
            justify-content: flex-end;
            gap: 8px;
            margin-top: 20px;
            padding-top: 16px;
            border-top: 1px solid var(--divider-color);
        }
        .action-btn {
            background: none;
            border: 1px solid var(--divider-color);
            border-radius: 4px;
            padding: 8px 16px;
            font-size: 0.85rem;
            font-weight: 500;
            font-family: inherit;
            cursor: pointer;
            transition: background 150ms ease;
        }
        .action-btn:disabled {
            opacity: 0.5;
            cursor: default;
        }
        .cancel-btn {
            background: transparent;
            color: var(--secondary-text-color);
        }
        .cancel-btn:hover:not(:disabled) {
            background: var(--secondary-background-color);
        }
        .create-btn {
            background: #2e7d32;
            color: #fff;
            border-color: #2e7d32;
        }
        .create-btn:hover:not(:disabled) {
            opacity: 0.9;
        }
    `;
}

declare global {
    interface HTMLElementTagNameMap {
        "ir-add-device-dialog": IrAddDeviceDialog;
    }
}
