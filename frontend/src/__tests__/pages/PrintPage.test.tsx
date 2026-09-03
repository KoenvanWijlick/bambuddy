/**
 * Tests for the one-click auto-print flow page (docs/auto-print-pipeline-spec.md).
 *
 * The mesh/toolpath viewers need WebGL, which jsdom has no answer for -- like
 * GCodeViewerPage.test.tsx, they're mocked out here; this file only cares
 * about the form, the options it's populated from, and the polling/stage
 * behaviour driven by the `/auto-print/` endpoints.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintPage } from '../../pages/PrintPage';

vi.mock('../../components/ModelViewer', () => ({
  ModelViewer: ({ url }: { url: string }) => <div data-testid="model-viewer" data-url={url} />,
}));
vi.mock('../../components/GcodeToolpathViewer', () => ({
  // A button surfaces onReady so tests can fire it explicitly -- the summary
  // timing tests need to assert what the page shows *before* it fires too.
  GcodeToolpathViewer: ({ gcodeUrl, onReady }: { gcodeUrl: string; onReady?: () => void }) => (
    <div data-testid="toolpath-viewer" data-url={gcodeUrl}>
      {onReady && (
        <button type="button" onClick={onReady}>
          mark toolpath ready
        </button>
      )}
    </div>
  ),
}));

const mockOptions = {
  filaments: [
    { filament_type: 'PLA', color_hex: '#FF0000', color_name: 'Red', printer_ids: [1], tray_count: 1 },
    { filament_type: 'PLA', color_hex: '#0000FF', color_name: 'Blue', printer_ids: [1], tray_count: 1 },
    { filament_type: 'PETG', color_hex: '#00FF00', color_name: 'Green', printer_ids: [1], tray_count: 1 },
  ],
  quality_tiers: [
    { tier: 'Standard', layer_heights: [0.2] },
    { tier: 'Fine', layer_heights: [0.12] },
  ],
  defaults: { quality: 'Standard', layer_height: 0.2 },
};

function stlFile(name = 'part.stl') {
  return new File(['solid'], name, { type: 'model/stl' });
}

// A flow at the new post-slice pause: sliced_library_file_id, gcode_preview_url
// and estimate are populated, but queue_item_id stays null until approval.
function awaitingApprovalFlow(id: number) {
  return {
    id,
    stage: 'awaiting_approval',
    stage_detail: '',
    progress: 85,
    error: null,
    library_file_id: 7,
    sliced_library_file_id: 8,
    queue_item_id: null,
    printer: {
      id: 1,
      name: 'Test Printer',
      model: 'X1C',
      nozzle_diameter: 0.4,
      reason: 'Idle, red PLA in AMS slot 1',
    },
    presets: {
      printer: 'Bambu Lab X1 Carbon 0.4 nozzle',
      process: '0.20mm Standard @BBL X1C',
      filament: 'Bambu PLA Basic @BBL X1C',
      bed_type: 'Textured PEI Plate',
      brim: 'Inner + outer, 5 mm',
    },
    estimate: { print_time_seconds: 1746, filament_used_g: 4.43, filament_used_mm: 1474.77 },
    model_preview_url: '/api/v1/library/files/7/model',
    gcode_preview_url: '/api/v1/library/files/8/gcode',
  };
}

// Drives the form to the point of clicking "Print" -- shared by every test
// that needs a flow started.
async function submitPrint(user: ReturnType<typeof userEvent.setup>) {
  const file = stlFile();
  const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
  await user.upload(fileInput, file);

  const typeSelect = await screen.findByLabelText('Type');
  await waitFor(() => expect(within(typeSelect).getAllByRole('option').length).toBe(2));
  await user.selectOptions(typeSelect, 'PLA');
  await waitFor(() => expect(screen.getByLabelText('Color')).toHaveValue('#FF0000'));

  await user.click(screen.getByRole('button', { name: 'Print' }));
}

describe('PrintPage', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/auto-print/options', () => HttpResponse.json(mockOptions)),
    );
  });

  describe('rendering', () => {
    it('renders the heading and subtitle', async () => {
      render(<PrintPage />);
      expect(screen.getByText('Start a new print')).toBeInTheDocument();
      expect(
        screen.getByText('Upload a file, choose your filament, adjust settings and start printing.'),
      ).toBeInTheDocument();
    });

    it('shows "No file selected" until a file is picked', async () => {
      render(<PrintPage />);
      expect(screen.getByText('No file selected')).toBeInTheDocument();
    });

    it('populates the filament type dropdown from GET /auto-print/options', async () => {
      render(<PrintPage />);
      const typeSelect = await screen.findByLabelText('Type');
      // Alphabetical, so this holds up whatever order the backend returns.
      await waitFor(() => {
        expect(within(typeSelect).getAllByRole('option').map((o) => o.textContent)).toEqual(['PETG', 'PLA']);
      });
    });

    it('defaults quality and layer height from the options endpoint', async () => {
      render(<PrintPage />);
      const qualitySelect = await screen.findByLabelText('Quality');
      await waitFor(() => expect(qualitySelect).toHaveValue('Standard'));
      const layerHeightSelect = screen.getByLabelText('Layer Height');
      expect(layerHeightSelect).toHaveValue('0.2');
    });

    it('shows the Print Summary and Printer Status placeholders', async () => {
      render(<PrintPage />);
      expect(screen.getByText('Print Summary')).toBeInTheDocument();
      // Printer row shows the em dash until a flow has picked one.
      expect(screen.getByText('Printer')).toBeInTheDocument();
    });
  });

  describe('file selection', () => {
    it('updates the filename line once a file is chosen', async () => {
      const user = userEvent.setup();
      render(<PrintPage />);

      const file = stlFile();
      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      await user.upload(fileInput, file);

      // Appears both on the dropzone's filename line and the Print Summary's
      // File row -- both are the point of this test.
      expect(screen.getAllByText('part.stl').length).toBeGreaterThanOrEqual(2);
    });
  });

  describe('color options follow the selected filament type', () => {
    it('shows only colours loaded for the selected type', async () => {
      const user = userEvent.setup();
      render(<PrintPage />);

      const typeSelect = await screen.findByLabelText('Type');
      // Alphabetical default: PETG before PLA.
      await waitFor(() => expect(typeSelect).toHaveValue('PETG'));

      const colorSelect = screen.getByLabelText('Color');
      await waitFor(() => {
        expect(within(colorSelect).getAllByRole('option').map((o) => o.textContent)).toEqual(['Green']);
      });

      await user.selectOptions(typeSelect, 'PLA');
      await waitFor(() => {
        expect(within(colorSelect).getAllByRole('option').map((o) => o.textContent)).toEqual(['Red', 'Blue']);
      });
    });
  });

  describe('submitting a print', () => {
    it('POSTs to /auto-print/, polls the flow, and shows the queued success state', async () => {
      const user = userEvent.setup();
      // Not read via request.formData() here: jsdom's File (from `new File()`
      // in this test) fails undici's internal multipart-part validation when
      // MSW parses it, turning the handler into an opaque 500 -- the same
      // reason the existing multipart-upload tests in this repo
      // (FileUploadModal.test.tsx) never call request.formData() either.
      server.use(
        http.post('/api/v1/auto-print/', () => HttpResponse.json({ id: 42, stage: 'pending' }, { status: 202 })),
        http.get('/api/v1/auto-print/42', () =>
          HttpResponse.json({
            id: 42,
            stage: 'queued',
            stage_detail: '',
            progress: 100,
            error: null,
            library_file_id: 7,
            sliced_library_file_id: 8,
            queue_item_id: 99,
            printer: {
              id: 1,
              name: 'Test Printer',
              model: 'X1C',
              nozzle_diameter: 0.4,
              reason: 'Idle, red PLA in AMS slot 1',
            },
            presets: {
              printer: 'Bambu Lab X1 Carbon 0.4 nozzle',
              process: '0.20mm Standard @BBL X1C',
              filament: 'Bambu PLA Basic @BBL X1C',
              bed_type: 'Textured PEI Plate',
            },
            estimate: { print_time_seconds: 1746, filament_used_g: 4.43, filament_used_mm: 1474.77 },
            model_preview_url: '/api/v1/library/files/7/model',
            gcode_preview_url: '/api/v1/library/files/8/gcode',
          }),
        ),
      );

      render(<PrintPage />);

      const file = stlFile();
      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      await user.upload(fileInput, file);

      const typeSelect = await screen.findByLabelText('Type');
      await waitFor(() => expect(within(typeSelect).getAllByRole('option').length).toBe(2));
      await user.selectOptions(typeSelect, 'PLA');
      await waitFor(() => expect(screen.getByLabelText('Color')).toHaveValue('#FF0000'));

      await user.click(screen.getByRole('button', { name: 'Print' }));

      await waitFor(() => {
        expect(screen.getByText('Added to the print queue.')).toBeInTheDocument();
      });
      expect(screen.getByRole('link', { name: /View queue/ })).toHaveAttribute('href', '/queue');
      // Toolpath viewer takes over once a gcode preview is available.
      expect(screen.getByTestId('toolpath-viewer')).toHaveAttribute(
        'data-url',
        '/api/v1/library/files/8/gcode',
      );
      // Printer Status card shows the auto-chosen printer and its reason.
      expect(screen.getByText('Idle, red PLA in AMS slot 1')).toBeInTheDocument();
    });

    it('shows the error inline and keeps the form filled on failure, with a working retry', async () => {
      const user = userEvent.setup();

      server.use(
        http.post('/api/v1/auto-print/', () =>
          HttpResponse.json({ id: 43, stage: 'pending' }, { status: 202 }),
        ),
        http.get('/api/v1/auto-print/43', () =>
          HttpResponse.json({
            id: 43,
            stage: 'failed',
            stage_detail: '',
            progress: 0,
            error: 'No idle printer has PLA loaded.',
            library_file_id: null,
            sliced_library_file_id: null,
            queue_item_id: null,
            printer: null,
            presets: null,
            estimate: { print_time_seconds: null, filament_used_g: null, filament_used_mm: null },
            model_preview_url: null,
            gcode_preview_url: null,
          }),
        ),
      );

      render(<PrintPage />);

      const file = stlFile();
      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      await user.upload(fileInput, file);

      const typeSelect = await screen.findByLabelText('Type');
      await waitFor(() => expect(within(typeSelect).getAllByRole('option').length).toBe(2));
      await user.selectOptions(typeSelect, 'PLA');
      await waitFor(() => expect(screen.getByLabelText('Color')).toHaveValue('#FF0000'));

      await user.click(screen.getByRole('button', { name: 'Print' }));

      await waitFor(() => {
        expect(screen.getByText('No idle printer has PLA loaded.')).toBeInTheDocument();
      });
      // The form stays filled for retry.
      expect(screen.getAllByText('part.stl').length).toBeGreaterThanOrEqual(1);

      await user.click(screen.getByRole('button', { name: 'Retry' }));
      expect(screen.queryByText('No idle printer has PLA loaded.')).not.toBeInTheDocument();
      // Retrying re-enables the Print button.
      expect(screen.getByRole('button', { name: 'Print' })).not.toBeDisabled();
    });
  });

  describe('approval gate', () => {
    it('pauses at awaiting_approval with nothing queued, offering Approve and Discard', async () => {
      const user = userEvent.setup();
      server.use(
        http.post('/api/v1/auto-print/', () => HttpResponse.json({ id: 50, stage: 'pending' }, { status: 202 })),
        http.get('/api/v1/auto-print/50', () => HttpResponse.json(awaitingApprovalFlow(50))),
      );

      render(<PrintPage />);
      await submitPrint(user);

      await waitFor(() => {
        expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
      });
      expect(screen.getByRole('button', { name: 'Discard' })).toBeInTheDocument();
      // Unmistakable: nothing has been queued yet.
      expect(
        screen.getByText(
          'Sliced and ready. Nothing has been queued yet — review the G-code below, then approve to add it to the print queue or discard to cancel.',
        ),
      ).toBeInTheDocument();
      expect(screen.getByText('Not queued yet — review below')).toBeInTheDocument();
      expect(screen.queryByText('Added to the print queue.')).not.toBeInTheDocument();
      // The G-code preview, not the mesh preview, is what's shown at this stage.
      expect(screen.getByTestId('toolpath-viewer')).toHaveAttribute(
        'data-url',
        '/api/v1/library/files/8/gcode',
      );
    });

    it('approve calls the approve endpoint and reaches the queued success state', async () => {
      const user = userEvent.setup();
      server.use(
        http.post('/api/v1/auto-print/', () => HttpResponse.json({ id: 51, stage: 'pending' }, { status: 202 })),
        http.get('/api/v1/auto-print/51', () => HttpResponse.json(awaitingApprovalFlow(51))),
        http.post('/api/v1/auto-print/51/approve', () =>
          HttpResponse.json({
            ...awaitingApprovalFlow(51),
            stage: 'queued',
            progress: 100,
            queue_item_id: 99,
          }),
        ),
      );

      render(<PrintPage />);
      await submitPrint(user);

      await waitFor(() => screen.getByRole('button', { name: 'Approve' }));
      await user.click(screen.getByRole('button', { name: 'Approve' }));

      await waitFor(() => {
        expect(screen.getByText('Added to the print queue.')).toBeInTheDocument();
      });
      expect(screen.getByRole('link', { name: /View queue/ })).toHaveAttribute('href', '/queue');
      expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    });

    it('discard reaches the discarded state without queueing anything', async () => {
      const user = userEvent.setup();
      server.use(
        http.post('/api/v1/auto-print/', () => HttpResponse.json({ id: 52, stage: 'pending' }, { status: 202 })),
        http.get('/api/v1/auto-print/52', () => HttpResponse.json(awaitingApprovalFlow(52))),
        http.post('/api/v1/auto-print/52/discard', () =>
          HttpResponse.json({
            ...awaitingApprovalFlow(52),
            stage: 'discarded',
          }),
        ),
      );

      render(<PrintPage />);
      await submitPrint(user);

      await waitFor(() => screen.getByRole('button', { name: 'Discard' }));
      await user.click(screen.getByRole('button', { name: 'Discard' }));

      await waitFor(() => {
        expect(screen.getByText('Discarded. Nothing was added to the print queue.')).toBeInTheDocument();
      });
      expect(screen.queryByText('Added to the print queue.')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
      // A way to start over.
      const startOver = screen.getByRole('button', { name: /Start a new print/ });
      expect(startOver).toBeInTheDocument();
      await user.click(startOver);
      expect(screen.queryByText('Discarded. Nothing was added to the print queue.')).not.toBeInTheDocument();
    });
  });

  describe('brim control', () => {
    it('defaults on at 5mm and disables the width input when toggled off', async () => {
      const user = userEvent.setup();
      render(<PrintPage />);

      await user.click(screen.getByRole('button', { name: 'Advanced Settings' }));

      const brimToggle = screen.getByRole('switch', { name: 'Inner + outer brim' });
      expect(brimToggle).toHaveAttribute('aria-checked', 'true');

      const widthInput = screen.getByLabelText('Brim width');
      expect(widthInput).toHaveValue(5);
      expect(widthInput).not.toBeDisabled();

      await user.click(brimToggle);
      expect(brimToggle).toHaveAttribute('aria-checked', 'false');
      expect(widthInput).toBeDisabled();
    });
  });

  describe('summary timing', () => {
    it('withholds estimated time and filament usage until the toolpath viewer signals ready', async () => {
      const user = userEvent.setup();
      server.use(
        http.post('/api/v1/auto-print/', () => HttpResponse.json({ id: 53, stage: 'pending' }, { status: 202 })),
        http.get('/api/v1/auto-print/53', () => HttpResponse.json(awaitingApprovalFlow(53))),
      );

      render(<PrintPage />);
      await submitPrint(user);

      await waitFor(() => screen.getByTestId('toolpath-viewer'));

      // File / Filament / Color / Layer Height / Printer are known before
      // slicing and show immediately.
      expect(screen.getAllByText('part.stl').length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText('Test Printer').length).toBeGreaterThanOrEqual(1);

      // Estimated Print Time and Filament Usage wait on the preview.
      expect(screen.getAllByText('Rendering preview…').length).toBe(2);
      expect(screen.queryByText('29m')).not.toBeInTheDocument();
      expect(screen.queryByText('4.4 g')).not.toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: 'mark toolpath ready' }));

      await waitFor(() => {
        expect(screen.queryByText('Rendering preview…')).not.toBeInTheDocument();
      });
      expect(screen.getByText('29m')).toBeInTheDocument();
      expect(screen.getByText('4.4 g')).toBeInTheDocument();
    });
  });
});
