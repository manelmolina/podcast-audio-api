$(function () {
  const $status = $('#status');

  $('.tab').on('click', function () {
    const panelId = $(this).data('panel');
    $('.tab').removeClass('active').attr('aria-selected', 'false');
    $(this).addClass('active').attr('aria-selected', 'true');
    $('.tab-panel').prop('hidden', true);
    $('#' + panelId).prop('hidden', false);
    $status.prop('hidden', true).removeClass('error').empty();
  });

  $('.merge-form').on('submit', function (event) {
    event.preventDefault();

    const form = this;
    const $button = $(form).find('button[type="submit"]');
    const originalLabel = $button.text();
    $button.prop('disabled', true).attr('aria-busy', 'true').text('Procesando…');
    $status.prop('hidden', false).removeClass('error').text(
      'Subiendo y procesando los audios. No cierres esta pestaña.'
    );

    $.ajax({
      url: form.action,
      method: 'POST',
      data: new FormData(form),
      processData: false,
      contentType: false,
      xhrFields: { responseType: 'blob' }
    }).done(function (blob, _status, xhr) {
      const disposition = xhr.getResponseHeader('Content-Disposition') || '';
      const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
      const basicMatch = disposition.match(/filename=["']?([^"';]+)/i);
      const match = utf8Match || basicMatch;
      const filename = match ? decodeURIComponent(match[1].replace(/["']/g, '')) : 'episodio-final.mp3';
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      form.reset();
      $status.text('Audio creado. La descarga ha comenzado.');
    }).fail(function (xhr) {
      const fallback = 'No se pudo crear el audio.';
      if (xhr.response instanceof Blob) {
        xhr.response.text().then(function (text) {
          try {
            $status.addClass('error').text(JSON.parse(text).error || fallback);
          } catch (_error) {
            $status.addClass('error').text(fallback);
          }
        });
      } else {
        $status.addClass('error').text(fallback);
      }
    }).always(function () {
      $button.prop('disabled', false).attr('aria-busy', 'false').text(originalLabel);
    });
  });
});
