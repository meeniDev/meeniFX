#version 330
// SAMPLING
uniform vec2 center;
uniform float strength, reach, threshold, falloff;
uniform int samples;
uniform vec3 rayColor;
uniform bool additive;
out vec4 result;
void main() {
    vec2 p = gl_FragCoord.xy-.5;
    vec4 acc = vec4(0);
    float total = 0.;
    for (int i=0;i<64;i++) {
        if (i>=samples) break;
        float step = float(i+1)/float(samples);
        vec4 pixel = samplePixel(iChannel0,p+(center-p)*reach*step,0,false);
        float brightness = dot(pixel.rgb/max(pixel.a,1e-8),vec3(.2126,.7152,.0722));
        float mask = clamp((brightness-threshold)/max(1e-5,1.-threshold),0.,1.);
        float weight = exp(-falloff*step);
        acc += pixel*mask*weight;
        total += weight;
    }
    vec4 rays = acc*(strength/max(total,1e-8));
    vec4 base = texelFetch(iChannel0,ivec2(gl_FragCoord.xy),0);
    vec3 light = clamp(rays.rgb*rayColor,0.,1.);
    vec3 rgb = additive ? base.rgb+light : 1.-(1.-base.rgb)*(1.-light);
    result = vec4(rgb,1.-(1.-base.a)*(1.-clamp(rays.a,0.,1.)));
}
